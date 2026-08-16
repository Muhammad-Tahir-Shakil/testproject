"""Explainable deterministic vendor scoring with an optional model blend.

The deterministic rules are the safe baseline: explicit weights are easier to
audit than an untrained black-box model. A validated model can contribute a
*bounded* correction through ``model_predictor`` without changing the API or
event contracts, and without ever being able to promote an ineligible vendor.

Two things are kept deliberately separate:

* **score** answers "how good is this vendor for this job" (ranking).
* **confidence** answers "how much should we trust that answer" (authority).

Conflating them is the usual way an AI dispatch system ends up auto-assigning
on thin evidence, so they take different inputs. See ``_confidence``.
"""

from dataclasses import dataclass
from typing import Callable

from .models import Job, Recommendation, ScoreFactors, VendorProfile


MODEL_VERSION = "rules-v1"

# Below this, the top recommendation is advisory only.
CONFIDENCE_THRESHOLD = 0.45

# Points (on the 0-100 scale) that rank 1 must beat rank 2 by before the
# ranking is treated as decisive. Inside this band the two vendors are
# effectively tied and a human should pick.
MARGIN_THRESHOLD = 2.0

# Risk levels that never auto-dispatch, regardless of score or confidence.
# This mirrors the policy stated in answers.md section 1.
HUMAN_APPROVAL_RISK_LEVELS = frozenset({"high"})

# Evidence model. `sample_size` saturates: ~20 completed jobs buys most of the
# available credit and more has diminishing returns. `data_age_hours` decays on
# a one-week scale because capacity and response times go stale quickly.
EVIDENCE_SATURATION_JOBS = 20.0
EVIDENCE_HALF_LIFE_HOURS = 168.0

# Applied when a profile reports no provenance at all. Deliberately weak.
UNKNOWN_EVIDENCE_PRIOR = 0.5

# Share of confidence a candidate keeps when its history is worthless. With a
# 0.45 threshold this means a perfect requirement fit backed by no evidence
# scores 0.25 and still routes to a human -- which is the intended behaviour.
EVIDENCE_FLOOR = 0.25

# The weights are deliberately visible so an operator can review the tradeoff.
WEIGHTS = {
    "availability": 0.10,
    "capacity": 0.10,
    "skill_match": 0.20,
    "region_match": 0.15,
    "completion_rate": 0.15,
    "similar_job_rate": 0.10,
    "rework_history": 0.10,
    "sla_fit": 0.05,
    "risk_fit": 0.05,
}

# Factors that act as eligibility gates rather than preferences. Confidence is
# limited by the weakest of these, so a vendor cannot compensate for a missing
# skill by being fast and cheap.
GATING_FACTORS = ("skill_match", "region_match", "risk_fit", "sla_fit")

# Score applied to region_match when the vendor does not serve the job region.
# This is a flat out-of-region penalty, not an adjacency model: the platform
# has no region graph, so every non-match is treated identically. A real
# deployment would replace this with travel time from a routing service.
OUT_OF_REGION_SCORE = 0.2

TEXT_SKILL_ALIASES = {
    "hvac": ("hvac", "air conditioning", "air-conditioning", "chiller", "cooling"),
    "refrigeration": ("refrigeration", "compressor", "condenser", "chiller"),
    "electrical": (
        "electrical",
        "electric",
        "wiring",
        "circuit",
        "power",
        "switchgear",
        "transfer switch",
    ),
    "plumbing": ("plumbing", "pipe", "piping", "water leak", "riser", "drain"),
    "elevator": ("elevator", "lift"),
    "controls": ("controls", "controller", "interlock", "automation", "bms"),
    "solar": ("solar", "photovoltaic", "pv array"),
    "battery": ("battery", "bess", "energy storage"),
    "generator": ("generator", "genset", "standby"),
    "fire safety": ("fire", "suppression", "smoke"),
    "water treatment": ("water treatment", "water quality"),
}


@dataclass(frozen=True)
class ScoredVendor:
    vendor: VendorProfile
    factors: ScoreFactors
    score: float
    rule_score: float
    model_score: float | None
    confidence: float
    requirement_fit: float
    evidence_strength: float


@dataclass(frozen=True)
class ScoringResult:
    """Ranked output plus the signals that decide who is allowed to act on it."""

    recommendations: list[Recommendation]
    decision_margin: float | None
    review_reasons: list[str]

    @property
    def requires_human_decision(self) -> bool:
        return bool(self.review_reasons)


def _ratio_match(required: list[str], available: list[str]) -> float:
    """Return case-insensitive skill coverage, treating no requirements as fit."""

    if not required:
        return 1.0
    available_values = {value.casefold() for value in available}
    matched = sum(skill.casefold() in available_values for skill in required)
    return matched / len(required)


def _effective_required_skills(job: Job) -> list[str]:
    """Combine explicit skills with service skills found in job text.

    Dispatchers often edit only a title or description while triaging. Matching
    a fixed vocabulary lets that affect ranking without putting a black-box text
    model inside an auditable scorer, and because only vocabulary terms are
    extracted, raw text never reaches ScoreFactors -- which is what keeps
    free-text PII out of the feature path.
    """

    text = " ".join(
        value for value in (job.job_type, job.title, job.details) if value
    ).casefold()
    skills = list(job.required_skills)
    normalized = {skill.casefold() for skill in skills}
    for skill, aliases in TEXT_SKILL_ALIASES.items():
        if skill not in normalized and any(alias in text for alias in aliases):
            skills.append(skill)
            normalized.add(skill)
    return skills


def inferred_skills(job: Job) -> list[str]:
    """Return only the service skills inferred from title and details."""

    explicit = {skill.casefold() for skill in job.required_skills}
    return [
        skill
        for skill in _effective_required_skills(job)
        if skill.casefold() not in explicit
    ]


def _sla_fit(job: Job, vendor: VendorProfile) -> float:
    if vendor.avg_response_hours <= job.sla_hours:
        return 1.0
    overage = vendor.avg_response_hours - job.sla_hours
    return max(0.0, 1.0 - (overage / job.sla_hours))


def _calculate_factors(job: Job, vendor: VendorProfile) -> ScoreFactors:
    """Convert raw profile values into normalized score factors."""

    return ScoreFactors(
        availability=1.0 if vendor.available_capacity > 0 else 0.0,
        capacity=min(vendor.available_capacity / max(vendor.capacity_total, 1), 1.0),
        skill_match=_ratio_match(_effective_required_skills(job), vendor.skills),
        region_match=1.0
        if job.region.casefold() in {r.casefold() for r in vendor.service_regions}
        else OUT_OF_REGION_SCORE,
        completion_rate=vendor.completion_rate,
        similar_job_rate=vendor.similar_job_rate,
        rework_history=1.0 - vendor.rework_rate,
        sla_fit=_sla_fit(job, vendor),
        risk_fit=1.0 if job.risk_level in vendor.risk_levels_supported else 0.25,
    )


def _weighted_score(factors: ScoreFactors) -> float:
    values = factors.model_dump()
    return sum(values[name] * weight for name, weight in WEIGHTS.items())


def requirement_fit(factors: ScoreFactors) -> float:
    """Return the weakest gating factor.

    ``min`` rather than an average is the point: a vendor perfect on three gates
    and absent on the fourth is not 75% suitable, it is unsuitable.
    """

    values = factors.model_dump()
    return min(values[name] for name in GATING_FACTORS)


def evidence_strength(vendor: VendorProfile) -> float:
    """Return how far the vendor's performance rates can be relied on.

    Ignores how *good* the rates are, only how well supported: 100% completion
    over two jobs a year ago is weaker than 90% over two hundred today.
    """

    if vendor.sample_size <= 0:
        volume = UNKNOWN_EVIDENCE_PRIOR
    else:
        volume = vendor.sample_size / (vendor.sample_size + EVIDENCE_SATURATION_JOBS)
    recency = 1.0 / (1.0 + (vendor.data_age_hours / EVIDENCE_HALF_LIFE_HOURS))
    return round(volume * recency, 6)


def _confidence(factors: ScoreFactors, vendor: VendorProfile) -> float:
    """Estimate how much authority this recommendation should carry.

    Note what is absent: the score. An earlier version averaged the factors and
    blended in the score, making confidence a restatement of the ranking --
    structurally incapable of being low for a high-scoring vendor, so
    abstention almost never fired.

    Fit *gates* rather than averages: a thoroughly evidenced inability to do the
    job is not a reason for confidence, so zero fit yields zero confidence.
    Evidence is weighted heavily enough to pull even a full-fit candidate below
    the threshold on its own, because a vendor that meets every requirement but
    has no verifiable history is exactly the case that should reach a human.
    """

    fit = requirement_fit(factors)
    evidence = evidence_strength(vendor)
    return round(fit * (EVIDENCE_FLOOR + (1.0 - EVIDENCE_FLOOR) * evidence), 3)


def _percent(value: float) -> str:
    return f"{round(value * 100):.0f}%"


def _join(reasons: list[str]) -> str:
    if len(reasons) == 1:
        return reasons[0]
    if len(reasons) == 2:
        return " and ".join(reasons)
    return ", ".join(reasons[:-1]) + ", and " + reasons[-1]


def _rationale(
    rank: int, vendor: VendorProfile, factors: ScoreFactors, job: Job
) -> str:
    """Build a deterministic explanation from the strongest positive factors."""

    reasons: list[str] = []
    if factors.skill_match >= 0.8:
        reasons.append(f"{_percent(factors.skill_match)} skill match")
    text_skills = inferred_skills(job)
    if text_skills:
        matched_text_skills = [
            skill
            for skill in text_skills
            if skill.casefold() in {value.casefold() for value in vendor.skills}
        ]
        if matched_text_skills:
            reasons.append("title/details align on " + ", ".join(matched_text_skills))
    if factors.region_match >= 1.0:
        reasons.append("service-region coverage")
    if factors.availability >= 1.0:
        reasons.append("current availability")
    if factors.completion_rate >= 0.8:
        reasons.append(f"{_percent(factors.completion_rate)} completion rate")
    if factors.similar_job_rate >= 0.8:
        reasons.append("strong similar-job performance")
    if factors.rework_history >= 0.8:
        reasons.append("low rework history")
    if factors.sla_fit >= 1.0:
        reasons.append("response time within SLA")

    if not reasons:
        reasons.append("best available combination of score factors")

    return f"{vendor.name} ranked #{rank} due to {_join(reasons)}."


def decision_margin(scored: list[ScoredVendor]) -> float | None:
    """Return the score gap between the best and second-best eligible vendor.

    Computed over every eligible vendor rather than the returned ``top_k``, so
    a ``top_k`` of 1 still reports a real margin.
    """

    if len(scored) < 2:
        return None
    return round(scored[0].score - scored[1].score, 2)


def review_reasons(
    job: Job,
    recommendations: list[Recommendation],
    margin: float | None,
    margin_threshold: float = MARGIN_THRESHOLD,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> list[str]:
    """List every policy reason a human must confirm this dispatch.

    An empty list is the only condition under which the system is willing to
    describe itself as ready to auto-dispatch.
    """

    if not recommendations:
        return ["No eligible vendor has capacity for this job."]

    reasons: list[str] = []
    top = recommendations[0]
    if top.abstained:
        # Three decimals: at two, a confidence of 0.448 against a 0.45
        # threshold renders as "0.45 is below the 0.45 threshold".
        reasons.append(
            f"Top candidate confidence {top.confidence:.3f} is below the "
            f"{confidence_threshold:.2f} threshold (requirement fit "
            f"{top.requirement_fit:.2f}, evidence {top.evidence_strength:.2f})."
        )
    if job.risk_level in HUMAN_APPROVAL_RISK_LEVELS:
        reasons.append(
            f"Policy: {job.risk_level}-risk jobs are never dispatched without "
            "human approval."
        )
    if margin is not None and margin < margin_threshold:
        reasons.append(
            f"Close call: rank 1 leads rank 2 by only {margin:.2f} points "
            f"(threshold {margin_threshold:.2f})."
        )
    return reasons


def evaluate(
    job: Job,
    vendors: list[VendorProfile],
    top_k: int = 3,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    model_predictor: Callable[[ScoreFactors], float] | None = None,
    model_weight: float = 0.2,
    margin_threshold: float = MARGIN_THRESHOLD,
) -> ScoringResult:
    """Rank vendors and report whether a human has to make the call.

    A local or hosted model can contribute a bounded score while the
    deterministic factors remain the primary guardrail: the model never sees
    ineligible vendors and cannot move a score by more than ``model_weight``.
    """

    scored: list[ScoredVendor] = []
    bounded_model_weight = min(max(model_weight, 0.0), 1.0)
    for vendor in vendors:
        # Inactive or full vendors are not safe dispatch candidates.
        if not vendor.active or vendor.available_capacity <= 0:
            continue
        factors = _calculate_factors(job, vendor)
        rule_unit = _weighted_score(factors)
        score_unit = rule_unit
        model_unit: float | None = None
        if model_predictor is not None:
            model_unit = min(max(model_predictor(factors), 0.0), 1.0)
            score_unit = (
                (1 - bounded_model_weight) * rule_unit
                + bounded_model_weight * model_unit
            )
        scored.append(
            ScoredVendor(
                vendor=vendor,
                factors=factors,
                score=round(score_unit * 100, 2),
                rule_score=round(rule_unit * 100, 2),
                model_score=None if model_unit is None else round(model_unit * 100, 2),
                confidence=_confidence(factors, vendor),
                requirement_fit=round(requirement_fit(factors), 6),
                evidence_strength=evidence_strength(vendor),
            )
        )

    # Vendor ID makes equal scores stable and repeatable across runs.
    scored.sort(key=lambda item: (-item.score, -item.confidence, item.vendor.vendor_id))

    recommendations = [
        Recommendation(
            vendor_id=item.vendor.vendor_id,
            vendor_name=item.vendor.name,
            rank=rank,
            score=item.score,
            rule_score=item.rule_score,
            model_score=item.model_score,
            confidence=item.confidence,
            requirement_fit=item.requirement_fit,
            evidence_strength=item.evidence_strength,
            abstained=item.confidence < confidence_threshold,
            score_factors=item.factors,
            rationale=_rationale(rank, item.vendor, item.factors, job),
        )
        for rank, item in enumerate(scored[:top_k], start=1)
    ]

    margin = decision_margin(scored)
    return ScoringResult(
        recommendations=recommendations,
        decision_margin=margin,
        review_reasons=review_reasons(
            job,
            recommendations,
            margin,
            margin_threshold=margin_threshold,
            confidence_threshold=confidence_threshold,
        ),
    )


def score_vendors(
    job: Job,
    vendors: list[VendorProfile],
    top_k: int = 3,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    model_predictor: Callable[[ScoreFactors], float] | None = None,
    model_weight: float = 0.2,
) -> list[Recommendation]:
    """Rank active vendors with capacity and return explainable recommendations."""

    return evaluate(
        job,
        vendors,
        top_k=top_k,
        confidence_threshold=confidence_threshold,
        model_predictor=model_predictor,
        model_weight=model_weight,
    ).recommendations
