"""Correlation trace for the live browser-to-AWS workflow."""

import json
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .models import JobCreatedEvent, RecommendationRequest
from .s3_errors import is_missing_key


TraceStatus = str


def now() -> datetime:
    return datetime.now(timezone.utc)


class TraceStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: TraceStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    detail: str | None = None


class RunTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    job_id: str
    status: str = "running"
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    steps: list[TraceStep]
    recommendation: dict[str, Any] | None = None
    error: str | None = None
    candidate_vendor_ids: list[str] = Field(default_factory=list)
    candidate_vendor_names: dict[str, str] = Field(default_factory=dict)
    candidate_count: int = 0
    eligible_candidate_count: int = 0
    decision_state: str = "pending"
    decision_margin: float | None = None
    review_reasons: list[str] = Field(default_factory=list)
    final_vendor_id: str | None = None
    final_vendor_name: str | None = None
    override: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None


class RunAcceptedResponse(BaseModel):
    request_id: str
    job_id: str
    status: str = "accepted"


class TraceStore(Protocol):
    def save(self, trace: RunTrace) -> None:
        ...

    def get(self, request_id: str) -> RunTrace | None:
        ...


class AwsRunTraceStore:
    """Persist trace state as small JSON objects in the existing audit bucket."""

    def __init__(self, bucket: str, client: Any | None = None) -> None:
        if not bucket:
            raise ValueError("AUDIT_BUCKET is required")
        self.bucket = bucket
        self.prefix = "runs"
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.client = client

    def key(self, request_id: str) -> str:
        return f"{self.prefix}/{request_id}.json"

    def save(self, trace: RunTrace) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self.key(trace.request_id),
            Body=trace.model_dump_json().encode("utf-8"),
            ContentType="application/json",
        )

    def get(self, request_id: str) -> RunTrace | None:
        from botocore.exceptions import ClientError

        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=self.key(request_id)
            )
        except ClientError as error:
            if is_missing_key(error, bucket=self.bucket, key=self.key(request_id)):
                return None
            raise
        return RunTrace.model_validate_json(response["Body"].read())


def initial_trace(request_id: str, job_id: str) -> RunTrace:
    names = [
        "Cognito",
        "API Gateway",
        "API Lambda",
        "SQS JobCreated",
        "Worker Lambda",
        "Rules + local ML view",
        "Recommendation event",
        "S3 audit",
        "Browser result",
        "Human decision",
    ]
    steps = [
        TraceStep(
            name=name,
            status="completed" if name in {"Cognito", "API Gateway", "API Lambda"} else "pending",
            completed_at=now()
            if name in {"Cognito", "API Gateway", "API Lambda"}
            else None,
        )
        for name in names
    ]
    return RunTrace(request_id=request_id, job_id=job_id, steps=steps)


def update_step(
    trace: RunTrace,
    name: str,
    status: str,
    detail: str | None = None,
) -> RunTrace:
    timestamp = now()
    for step in trace.steps:
        if step.name == name:
            if status == "running" and step.started_at is None:
                step.started_at = timestamp
            if status in {"completed", "failed"}:
                step.started_at = step.started_at or timestamp
                step.completed_at = timestamp
            step.status = status
            step.detail = detail
            break
    trace.updated_at = timestamp
    return trace


class AwsRunCoordinator:
    """Accept a browser request and enqueue the existing JobCreated event."""

    def __init__(self, queue, trace_store: TraceStore) -> None:
        self.queue = queue
        self.trace_store = trace_store

    def create(self, request: RecommendationRequest) -> RunAcceptedResponse:
        request_id = str(uuid4())
        eligible_vendors = [
            vendor
            for vendor in request.vendors
            if vendor.active and vendor.available_capacity > 0
        ]
        trace = initial_trace(request_id, request.job.job_id)
        trace.candidate_vendor_ids = [
            vendor.vendor_id for vendor in request.vendors
        ]
        trace.candidate_vendor_names = {
            vendor.vendor_id: vendor.name for vendor in request.vendors
        }
        trace.candidate_count = len(request.vendors)
        trace.eligible_candidate_count = len(eligible_vendors)
        trace = update_step(trace, "SQS JobCreated", "running")
        self.trace_store.save(trace)
        event = JobCreatedEvent(
            event_id=request_id,
            job=request.job,
            vendors=request.vendors,
            top_k=request.top_k,
        )
        try:
            self.queue.publish(event)
            trace = update_step(trace, "SQS JobCreated", "completed")
            trace.status = "running"
            self.trace_store.save(trace)
        except Exception as error:
            trace.status = "failed"
            trace.error = "Unable to enqueue JobCreated"
            update_step(trace, "SQS JobCreated", "failed", "enqueue failed")
            self.trace_store.save(trace)
            raise error
        return RunAcceptedResponse(request_id=request_id, job_id=request.job.job_id)
