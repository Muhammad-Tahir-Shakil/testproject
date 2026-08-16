"""API and domain contracts.

The models are intentionally small. They are the canonical contracts that can
later be shared between an HTTP adapter, Lambda, and message handlers.
"""

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


RiskLevel = Literal["low", "medium", "high"]
DecisionState = Literal[
    "ai_recommended",
    "manual_review_required",
    "ai_recommendation_confirmed",
    "human_overridden",
]
DecisionType = Literal["confirmed", "overridden"]
DecisionEventType = Literal[
    "VendorRecommendationConfirmed",
    "VendorRecommendationOverridden",
]


def utc_now() -> datetime:
    """Return a timezone-aware timestamp for event and audit records."""

    return datetime.now(timezone.utc)


class Job(BaseModel):
    """The job attributes needed for vendor matching."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1)
    customer_name: str = Field(default="Synthetic customer", min_length=1)
    site_name: str = Field(default="Unassigned site", min_length=1)
    asset_label: str = Field(default="Unspecified asset", min_length=1)
    job_type: str = Field(min_length=1)
    title: str = Field(default="", min_length=0)
    details: str = Field(default="", min_length=0)
    required_skills: list[str] = Field(default_factory=list)
    region: str = Field(min_length=1)
    sla_hours: float = Field(gt=0)
    risk_level: RiskLevel = "low"


class VendorProfile(BaseModel):
    """Vendor scorecard data supplied by the vendor/profile system."""

    model_config = ConfigDict(extra="forbid")

    vendor_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    service_regions: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    capacity_total: int = Field(ge=0)
    capacity_used: int = Field(ge=0)
    completion_rate: float = Field(ge=0, le=1)
    similar_job_rate: float = Field(ge=0, le=1)
    avg_response_hours: float = Field(ge=0)
    rework_rate: float = Field(ge=0, le=1)
    risk_levels_supported: list[RiskLevel] = Field(
        default_factory=lambda: ["low", "medium", "high"]
    )
    active: bool = True

    # Provenance of the performance rates above. These are deliberately NOT
    # scoring factors; they describe how much evidence those rates rest on,
    # which is what the confidence estimate consumes. Both default to
    # "unknown", and unknown provenance is treated as weak evidence so an
    # unverified profile biases toward human review rather than away from it.
    sample_size: int = Field(
        default=0,
        ge=0,
        description="Completed jobs behind completion_rate and similar_job_rate.",
    )
    data_age_hours: float = Field(
        default=0.0,
        ge=0,
        description="Age of the capacity and performance snapshot, in hours.",
    )

    @property
    def available_capacity(self) -> int:
        """Prevent a malformed profile from producing negative availability."""

        return max(self.capacity_total - self.capacity_used, 0)


class ScoreFactors(BaseModel):
    """Normalized, human-auditable components of a vendor score."""

    availability: float = Field(ge=0, le=1)
    capacity: float = Field(ge=0, le=1)
    skill_match: float = Field(ge=0, le=1)
    region_match: float = Field(ge=0, le=1)
    completion_rate: float = Field(ge=0, le=1)
    similar_job_rate: float = Field(ge=0, le=1)
    rework_history: float = Field(ge=0, le=1)
    sla_fit: float = Field(ge=0, le=1)
    risk_fit: float = Field(ge=0, le=1)


class Recommendation(BaseModel):
    """One ranked vendor recommendation with its explanation."""

    vendor_id: str
    vendor_name: str
    rank: int = Field(ge=1)
    score: float = Field(ge=0, le=100)
    rule_score: float = Field(default=0.0, ge=0, le=100)
    model_score: float | None = Field(default=None, ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    requirement_fit: float = Field(default=0.0, ge=0, le=1)
    evidence_strength: float = Field(default=0.0, ge=0, le=1)
    abstained: bool
    score_factors: ScoreFactors
    rationale: str = Field(min_length=1)


class RecommendationRequest(BaseModel):
    """Self-contained request used by the API and integration examples."""

    model_config = ConfigDict(extra="forbid")

    job: Job
    vendors: list[VendorProfile] = Field(min_length=1)
    top_k: int = Field(default=3, ge=1, le=5)


class RecommendationResponse(BaseModel):
    """Output consumed by Admin and Dispatch services."""

    request_id: str
    job_id: str
    event_type: Literal["VendorRecommendationGenerated"] = (
        "VendorRecommendationGenerated"
    )
    model_version: str
    status: Literal["recommendations_ready", "manual_review_required"]
    generated_at: datetime
    recommendations: list[Recommendation]
    candidate_count: int = Field(default=0, ge=0)
    eligible_candidate_count: int = Field(default=0, ge=0)
    excluded_candidate_count: int = Field(default=0, ge=0)
    decision_margin: float | None = Field(
        default=None,
        description="Score gap between rank 1 and rank 2; None when unranked.",
    )
    review_reasons: list[str] = Field(
        default_factory=list,
        description="Why a human must decide. Empty when auto-dispatch is allowed.",
    )
    decision_state: DecisionState = "manual_review_required"
    recommended_vendor_id: str | None = None
    recommended_vendor_name: str | None = None


class JobEvent(BaseModel):
    """Event envelope used to simulate the dispatch workflow."""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: Literal["JobCreated"] = "JobCreated"
    occurred_at: datetime = Field(default_factory=utc_now)
    job: Job
    vendors: list[VendorProfile] = Field(min_length=1)
    top_k: int = Field(default=3, ge=1, le=5)


# The concrete event used by this slice is JobCreated. Keeping the alias makes
# the canonical JobEvent name explicit for future event types.
JobCreatedEvent = JobEvent


class VendorRecommendationGeneratedEvent(BaseModel):
    """Event published after scoring completes."""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: Literal["VendorRecommendationGenerated"] = (
        "VendorRecommendationGenerated"
    )
    occurred_at: datetime = Field(default_factory=utc_now)
    payload: RecommendationResponse


class OverrideRequest(BaseModel):
    """A human decision that supersedes an advisory recommendation."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1)
    vendor_id: str = Field(min_length=1)
    reason: str = Field(min_length=3)
    actor_id: str = Field(min_length=1)
    request_id: str | None = None


class OverrideResponse(BaseModel):
    """Versioned final decision returned to the Admin service."""

    event_id: str
    event_type: DecisionEventType = "VendorRecommendationOverridden"
    job_id: str
    vendor_id: str
    reason: str
    actor_id: str
    recorded_at: datetime
    request_id: str | None = None
    decision_id: str = Field(default_factory=lambda: str(uuid4()))
    decision_version: int = Field(default=1, ge=1)
    decision_type: DecisionType = "overridden"
    idempotent: bool = False
    active: bool = True
    ai_vendor_id: str | None = None
    ai_vendor_name: str | None = None
    previous_vendor_id: str | None = None
    previous_vendor_name: str | None = None
    previous_rank: int | None = None
    previous_score: float | None = None
    final_vendor_name: str | None = None
    changed: bool = True
    revision_history: list["DecisionRevision"] = Field(default_factory=list)


class DecisionRevision(BaseModel):
    """One immutable final-decision revision for a job."""

    decision_id: str
    job_id: str
    request_id: str | None = None
    decision_version: int = Field(ge=1)
    decision_type: DecisionType
    ai_vendor_id: str | None = None
    ai_vendor_name: str | None = None
    final_vendor_id: str
    final_vendor_name: str | None = None
    reason: str
    actor_id: str
    recorded_at: datetime


class DecisionSnapshot(BaseModel):
    """Active decision plus all real revisions for one job."""

    job_id: str
    active: DecisionRevision
    revisions: list[DecisionRevision] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)


OverrideResponse.model_rebuild()
