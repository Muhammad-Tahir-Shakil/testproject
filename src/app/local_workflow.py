"""Local setup and workflow orchestration for the browser dashboard."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .decisions import snapshot_from_response
from .events import InMemoryEventBus, redact
from .local_ml import MODEL_VERSION, LocalModel
from .local_store import LocalStore
from .models import (
    JobCreatedEvent,
    OverrideRequest,
    OverrideResponse,
)
from .observability import get_logger
from .service import RecommendationService


# src-layout: this module is src/app/local_workflow.py, so the repository root
# is two levels up. Only the local dashboard reads these fixtures -- the AWS
# handlers never import this module, which is why data/ is not packaged into
# the Lambda artifact.
PROJECT_ROOT = Path(__file__).parents[2]
SAMPLE_PATH = PROJECT_ROOT / "data" / "sample.json"
TRAINING_PATH = PROJECT_ROOT / "data" / "training.json"

logger = get_logger(__name__)


class StoreEventBus:
    """Persist local events while preserving the regular event-bus contract."""

    def __init__(self, store: LocalStore) -> None:
        self.inner = InMemoryEventBus()
        self.store = store

    def publish(self, event: BaseModel) -> dict[str, Any]:
        record = self.inner.publish(event)
        self.store.save_event(
            event_id=record.get("event_id", "unknown"),
            event_type=record["event_type"],
            payload=record,
        )
        return record

    def list_events(self) -> list[dict[str, Any]]:
        return self.inner.list_events()


class StoreAuditSink:
    """Persist the same redacted audit shape used by the AWS sink.

    Redaction is identical to the S3 path on purpose: if the local audit view
    showed more than production does, the dashboard would be demonstrating a
    privacy posture the deployed system does not actually have.
    """

    def __init__(self, store: LocalStore) -> None:
        self.store = store

    def log(self, action: str, payload: BaseModel | dict[str, Any]) -> None:
        raw = (
            payload.model_dump(mode="json")
            if isinstance(payload, BaseModel)
            else payload
        )
        self.store.save_audit(action, redact(raw, retain_free_text=False))


class LocalWorkflow:
    """Local equivalent of setup, dispatch, recommendation, and override flow."""

    def __init__(self, db_path: Path, model_path: Path) -> None:
        self.store = LocalStore(Path(db_path))
        self.model = LocalModel(Path(model_path))
        # Never raises: a corrupt artifact is reported through model.metadata()
        # and cleared by re-running setup, rather than making every dashboard
        # route return 500 with no reachable recovery path.
        self.model.load()
        if self.model.load_error:
            logger.warning(
                "local.model_artifact_rejected",
                extra={"load_error": self.model.load_error},
            )
        self.event_bus = StoreEventBus(self.store)
        self.audit = StoreAuditSink(self.store)
        self.service = RecommendationService(
            event_bus=self.event_bus,
            audit_logger=self.audit,
            model_version=MODEL_VERSION,
            model_predictor=self.model.predict if self.model.ready else None,
        )

    @property
    def ready(self) -> bool:
        return bool(self.store.get_setting("setup_ready", False)) and self.model.ready

    def _sample(self) -> dict[str, Any]:
        return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))

    def sample_event(self) -> JobCreatedEvent:
        sample = self._sample()
        return JobCreatedEvent(
            job=sample["job"],
            vendors=sample["vendors"],
            top_k=sample.get("top_k", 3),
        )

    def setup(self) -> dict[str, Any]:
        event = self.sample_event()
        self.store.initialize()
        self.store.save_job(event.job.model_dump(mode="json"))
        for vendor in event.vendors:
            self.store.save_vendor(vendor.model_dump(mode="json"))
        metadata = self.model.train(TRAINING_PATH)
        self.service.model_predictor = self.model.predict
        self.store.set_setting("setup_ready", True)
        self.store.set_setting("setup_at", datetime.now(timezone.utc).isoformat())
        self.store.set_setting("model_version", self.model.version)
        self.audit.log(
            "LocalSetupCompleted",
            {"job_id": event.job.job_id, "vendor_count": len(event.vendors)},
        )
        logger.info(
            "local.setup_completed",
            extra={
                "vendor_count": len(event.vendors),
                "model_version": self.model.version,
                "training_rows": metadata.get("training_rows"),
            },
        )
        return {
            "ready": True,
            "job_id": event.job.job_id,
            "vendor_count": len(event.vendors),
            "model": metadata,
        }

    def process(self, event: JobCreatedEvent) -> Any:
        if not self.ready:
            self.setup()
        self.store.save_job(event.job.model_dump(mode="json"))
        for vendor in event.vendors:
            self.store.save_vendor(vendor.model_dump(mode="json"))
        self.store.save_event(
            event_id=event.event_id,
            event_type=event.event_type,
            payload=event.model_dump(mode="json"),
        )
        response = self.service.handle_job_created(event)
        self.store.save_recommendation(
            event.job.job_id, response.model_dump(mode="json")
        )
        return response

    def override(self, request: OverrideRequest) -> OverrideResponse:
        if not self.ready:
            self.setup()
        previous = self.store.latest_recommendation(request.job_id)
        previous_decision = self.store.get_decision(request.job_id)
        response = self.service.record_override(
            request,
            previous_recommendation=previous,
            previous_snapshot=previous_decision,
            final_vendor_name=self.store.vendor_name(request.vendor_id),
        )
        if not response.idempotent:
            self.store.save_decision(snapshot_from_response(response))
        return response

    def dashboard(self) -> dict[str, Any]:
        return self.store.dashboard(self.model.metadata())

    def reset(self) -> None:
        self.store.reset()
        if self.model.artifact_path.exists():
            self.model.artifact_path.unlink()
