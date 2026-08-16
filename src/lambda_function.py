"""AWS Lambda SQS worker for the JobCreated event."""

import os
from functools import lru_cache
from typing import Any

from app.events import AwsS3AuditLogger, AwsSqsEventBus
from app.idempotency import IdempotencyStore
from app.models import JobEvent
from app.observability import configure_logging, get_logger
from app.run_trace import AwsRunTraceStore, update_step
from app.scoring import CONFIDENCE_THRESHOLD, MODEL_VERSION
from app.service import RecommendationService


configure_logging()
logger = get_logger(__name__)


@lru_cache(maxsize=1)
def build_service() -> RecommendationService:
    """Reuse AWS clients across warm Lambda invocations."""

    return RecommendationService(
        event_bus=AwsSqsEventBus(
            recommendation_queue_url=os.environ["RECOMMENDATION_QUEUE_URL"],
        ),
        audit_logger=AwsS3AuditLogger(bucket=os.environ["AUDIT_BUCKET"]),
        model_version=os.getenv("MODEL_VERSION", MODEL_VERSION),
        confidence_threshold=float(
            os.getenv("CONFIDENCE_THRESHOLD", str(CONFIDENCE_THRESHOLD))
        ),
        model_predictor=build_model_predictor(),
    )


@lru_cache(maxsize=1)
def build_model_predictor():
    """Load the JSON model artifact when one is packaged with the function.

    Unset by default: the deployed stack runs the transparent rules baseline
    and the trained model stays local. Setting SCORING_MODEL_ARTIFACT to a
    packaged artifact path is all that is needed to enable blended scoring --
    the artifact format and predictor are identical in both environments.
    See docs/platform-decisions.md.
    """

    artifact_path = os.getenv("SCORING_MODEL_ARTIFACT")
    if not artifact_path:
        return None
    from pathlib import Path

    from app.local_ml import ModelArtifactError, load_predictor

    try:
        predictor = load_predictor(Path(artifact_path))
    except (ModelArtifactError, OSError, ValueError):
        # A bad artifact must degrade to rules, never take the worker down.
        logger.exception(
            "model.artifact_rejected", extra={"artifact_path": artifact_path}
        )
        return None
    logger.info("model.artifact_loaded", extra={"artifact_path": artifact_path})
    return predictor


@lru_cache(maxsize=1)
def build_trace_store() -> AwsRunTraceStore:
    return AwsRunTraceStore(bucket=os.environ["AUDIT_BUCKET"])


@lru_cache(maxsize=1)
def build_idempotency_store() -> IdempotencyStore:
    return IdempotencyStore(bucket=os.environ["AUDIT_BUCKET"])


def _audit_bucket_configured() -> bool:
    return bool(os.getenv("AUDIT_BUCKET"))


def _fail_trace(request_id: str | None, message_id: str) -> None:
    """Mark a run as failed so the browser stops polling a stuck trace.

    ``request_id`` is the JobCreated ``event_id``; the trace is keyed by it.
    An earlier version looked the trace up by the SQS ``messageId``, which is a
    different identifier, so no failure was ever recorded and the dashboard
    span at a 'running' step until it timed out.
    """

    if not request_id or not _audit_bucket_configured():
        return
    try:
        trace_store = build_trace_store()
        trace = trace_store.get(request_id)
        if trace is None:
            return
        trace.status = "failed"
        trace.error = "Worker failed while processing the event"
        trace_store.save(update_step(trace, "Worker Lambda", "failed", "retrying"))
    except Exception:
        logger.exception(
            "trace.failure_update_failed",
            extra={"request_id": request_id, "message_id": message_id},
        )


def handler(event: dict[str, Any], _context: Any) -> dict[str, list[dict[str, str]]]:
    """Process SQS records and retry only failed messages."""

    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown-message")
        request_id: str | None = None
        try:
            job_event = JobEvent.model_validate_json(record["body"])
            request_id = job_event.event_id

            # SQS is at-least-once. Claim the event before doing anything with
            # side effects so a redelivery cannot double-publish.
            if _audit_bucket_configured():
                claimed = build_idempotency_store().claim(
                    request_id, {"job_id": job_event.job.job_id}
                )
                if not claimed:
                    logger.info(
                        "job_created.duplicate_skipped",
                        extra={
                            "request_id": request_id,
                            "job_id": job_event.job.job_id,
                            "message_id": message_id,
                        },
                    )
                    continue

            trace_store = build_trace_store() if _audit_bucket_configured() else None
            trace = trace_store.get(request_id) if trace_store else None
            if trace_store and trace:
                update_step(trace, "Worker Lambda", "running")
                update_step(trace, "Rules + local ML view", "running")
                trace_store.save(trace)

            response = build_service().handle_job_created(job_event)

            if trace_store and trace:
                trace.recommendation = response.model_dump(mode="json")
                trace.decision_state = response.decision_state
                trace.final_vendor_id = response.recommended_vendor_id
                trace.final_vendor_name = response.recommended_vendor_name
                trace.review_reasons = list(response.review_reasons)
                trace.decision_margin = response.decision_margin
                update_step(
                    trace,
                    "Rules + local ML view",
                    "completed",
                    f"{response.model_version} completed",
                )
                update_step(trace, "Recommendation event", "completed")
                update_step(trace, "S3 audit", "completed")
                update_step(trace, "Worker Lambda", "completed")
                trace.status = "completed"
                # One write instead of six: each put_object was a round trip
                # and a chance for a concurrent override write to be lost.
                trace_store.save(trace)

            logger.info(
                "job_created.processed",
                extra={
                    "request_id": request_id,
                    "job_id": job_event.job.job_id,
                    "message_id": message_id,
                    "decision_state": getattr(response, "decision_state", None),
                },
            )
        except Exception:
            logger.exception(
                "job_created.failed",
                extra={"message_id": message_id, "request_id": request_id},
            )
            _fail_trace(request_id, message_id)
            failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}
