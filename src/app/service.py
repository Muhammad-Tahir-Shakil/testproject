"""Application service coordinating scoring, events, and audit records."""

from typing import Callable
from uuid import uuid4

from .decisions import create_decision
from .events import AuditLogger, AuditSink, EventBus, InMemoryEventBus
from .observability import get_logger
from .models import (
    JobCreatedEvent,
    OverrideRequest,
    OverrideResponse,
    DecisionSnapshot,
    RecommendationRequest,
    RecommendationResponse,
    ScoreFactors,
    VendorRecommendationGeneratedEvent,
    utc_now,
)
from .scoring import CONFIDENCE_THRESHOLD, MODEL_VERSION, evaluate


logger = get_logger(__name__)


class RecommendationService:
    """Keep domain workflow independent from the HTTP framework."""

    def __init__(
        self,
        event_bus: EventBus | None = None,
        audit_logger: AuditSink | None = None,
        model_version: str = MODEL_VERSION,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
        model_predictor: Callable[[ScoreFactors], float] | None = None,
        model_weight: float = 0.2,
    ) -> None:
        self.event_bus = event_bus or InMemoryEventBus()
        self.audit_logger = audit_logger or AuditLogger()
        self.model_version = model_version
        self.confidence_threshold = confidence_threshold
        self.model_predictor = model_predictor
        self.model_weight = model_weight

    def recommend(self, request: RecommendationRequest) -> RecommendationResponse:
        self.audit_logger.log("RecommendationInput", request)
        result = evaluate(
            request.job,
            request.vendors,
            top_k=request.top_k,
            confidence_threshold=self.confidence_threshold,
            model_predictor=self.model_predictor,
            model_weight=self.model_weight,
        )

        # A single source of truth for authority: the scorer lists every policy
        # reason a human must decide, and the status is derived from that list
        # rather than from the top score. Adding a new policy therefore cannot
        # be forgotten here.
        status = (
            "manual_review_required"
            if result.requires_human_decision
            else "recommendations_ready"
        )
        eligible_candidate_count = sum(
            vendor.active and vendor.available_capacity > 0
            for vendor in request.vendors
        )
        recommended = result.recommendations[0] if result.recommendations else None
        response = RecommendationResponse(
            request_id=str(uuid4()),
            job_id=request.job.job_id,
            model_version=self.model_version,
            status=status,
            generated_at=utc_now(),
            recommendations=result.recommendations,
            candidate_count=len(request.vendors),
            eligible_candidate_count=eligible_candidate_count,
            excluded_candidate_count=len(request.vendors) - eligible_candidate_count,
            decision_margin=result.decision_margin,
            review_reasons=result.review_reasons,
            decision_state=(
                "manual_review_required"
                if result.requires_human_decision
                else "ai_recommended"
            ),
            recommended_vendor_id=recommended.vendor_id if recommended else None,
            recommended_vendor_name=recommended.vendor_name if recommended else None,
        )
        self.audit_logger.log("RecommendationOutput", response)
        logger.info(
            "recommendation.generated",
            extra={
                "request_id": response.request_id,
                "job_id": response.job_id,
                "model_version": response.model_version,
                "decision_state": response.decision_state,
                "status": response.status,
                "top_vendor_id": response.recommended_vendor_id,
                "top_score": recommended.score if recommended else None,
                "top_confidence": recommended.confidence if recommended else None,
                "decision_margin": response.decision_margin,
                "review_reason_count": len(response.review_reasons),
                "candidate_count": response.candidate_count,
                "eligible_candidate_count": response.eligible_candidate_count,
            },
        )
        return response

    def handle_job_created(self, event: JobCreatedEvent) -> RecommendationResponse:
        """Process JobCreated and publish the downstream recommendation event."""

        self.audit_logger.log("JobCreatedInput", event)
        response = self.recommend(
            RecommendationRequest(
                job=event.job,
                vendors=event.vendors,
                top_k=event.top_k,
            )
        )
        generated_event = VendorRecommendationGeneratedEvent(payload=response)
        self.event_bus.publish(generated_event)
        self.audit_logger.log("VendorRecommendationGenerated", generated_event)
        return response

    def record_override(
        self,
        request: OverrideRequest,
        previous_recommendation: dict | None = None,
        previous_snapshot: DecisionSnapshot | None = None,
        final_vendor_name: str | None = None,
    ) -> OverrideResponse:
        """Record a confirmed or overridden final decision."""

        response, _snapshot = create_decision(
            request,
            previous_recommendation=previous_recommendation,
            previous_snapshot=previous_snapshot,
            final_vendor_name=final_vendor_name,
        )
        if response.idempotent:
            self.audit_logger.log("VendorDecisionIdempotent", response)
        else:
            self.event_bus.publish(response)
            self.audit_logger.log(response.event_type, response)
        logger.info(
            "decision.recorded",
            extra={
                "request_id": response.request_id,
                "job_id": response.job_id,
                "event_id": response.event_id,
                "model_version": self.model_version,
                "final_vendor_id": response.vendor_id,
                "previous_vendor_id": response.previous_vendor_id,
                "decision_type": response.decision_type,
                "decision_version": response.decision_version,
                "idempotent": response.idempotent,
                "changed": response.changed,
            },
        )
        return response
