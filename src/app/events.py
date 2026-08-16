"""Local and AWS event/audit adapters.

The in-memory implementations keep local development fast. The AWS
implementations use SQS and S3 without changing the domain service or its
contracts.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel


class EventBus(Protocol):
    """Outbound event contract shared by local and AWS implementations."""

    def publish(self, event: BaseModel) -> dict[str, Any]:
        ...

    def list_events(self) -> list[dict[str, Any]]:
        ...


class AuditSink(Protocol):
    """Audit contract shared by local and durable storage implementations."""

    def log(self, action: str, payload: BaseModel | dict[str, Any]) -> None:
        ...


class InMemoryEventBus:
    """Small event bus that makes the dispatch flow observable in tests."""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._lock = Lock()

    def publish(self, event: BaseModel) -> dict[str, Any]:
        record = event.model_dump(mode="json")
        with self._lock:
            self._events.append(record)
        return record

    def list_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)


class AuditOnlyEventBus:
    """No-op outbound bus for the API path.

    The API records recommendations and overrides in S3. Only the SQS worker
    publishes the generated recommendation event, so the API does not need
    SQS permissions.
    """

    def publish(self, event: BaseModel) -> dict[str, Any]:
        return event.model_dump(mode="json")

    def list_events(self) -> list[dict[str, Any]]:
        return []


# Identifiers that directly name a person or premises. Always removed.
SENSITIVE_KEYS = {
    "actor_email",
    "address",
    "customer_email",
    "customer_name",
    "customer_phone",
    "email",
    "phone",
    "site_name",
}

# Operator-authored free text. A dispatcher can and does paste a contact name,
# a phone number, or a gate code into these fields, so the durable audit trail
# cannot keep them by default. The decision stays explainable without them
# because the audit record still carries job_type, required_skills, the
# inferred service skills, every ScoreFactor, and the rationale string --
# that is, everything the scorer actually consumed. The raw text is retained
# only in the operational store, under the same access control as the job
# record itself.
FREE_TEXT_KEYS = {
    "asset_label",
    "details",
    "title",
}

REDACTED = "[REDACTED]"
REDACTED_TEXT = "[REDACTED_FREE_TEXT]"


def redact(value: Any, retain_free_text: bool = False) -> Any:
    """Redact PII recursively before writing an audit record.

    ``retain_free_text`` exists for local debugging only. It is never enabled
    on the AWS path; see ``AwsS3AuditLogger``.
    """

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            folded = key.casefold()
            if folded in SENSITIVE_KEYS:
                redacted[key] = REDACTED
            elif folded in FREE_TEXT_KEYS and not retain_free_text:
                redacted[key] = REDACTED_TEXT if item else item
            else:
                redacted[key] = redact(item, retain_free_text=retain_free_text)
        return redacted
    if isinstance(value, list):
        return [redact(item, retain_free_text=retain_free_text) for item in value]
    return value


class AuditLogger:
    """JSONL audit sink with an in-memory copy for inspection and tests."""

    def __init__(self, path: str | None = None, retain_free_text: bool = False) -> None:
        self.path = Path(path) if path else None
        self.retain_free_text = retain_free_text
        self.records: list[dict[str, Any]] = []
        self._lock = Lock()

    def log(self, action: str, payload: BaseModel | dict[str, Any]) -> None:
        raw = (
            payload.model_dump(mode="json")
            if isinstance(payload, BaseModel)
            else payload
        )
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "payload": redact(raw, retain_free_text=self.retain_free_text),
        }
        with self._lock:
            self.records.append(record)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as audit_file:
                    audit_file.write(json.dumps(record) + "\n")


class AwsSqsEventBus:
    """Publish generated events to durable AWS SQS queues.

    The queue URLs are injected by SAM. A fake client can be injected for
    offline tests, so no AWS call is needed during local development.
    """

    def __init__(
        self,
        recommendation_queue_url: str | None = None,
        override_queue_url: str | None = None,
        job_created_queue_url: str | None = None,
        client: Any | None = None,
    ) -> None:
        if not (recommendation_queue_url or job_created_queue_url):
            raise ValueError(
                "At least one of recommendation_queue_url or "
                "job_created_queue_url is required"
            )
        self.recommendation_queue_url = recommendation_queue_url
        # No override queue exists in template.yaml: overrides are durable
        # because they are written to the S3 audit trail and the run trace, and
        # nothing consumes them asynchronously yet. The parameter is kept so a
        # feedback consumer can be added without touching the service layer;
        # until then publish() is a no-op for override events.
        self.override_queue_url = override_queue_url
        self.job_created_queue_url = job_created_queue_url
        if client is None:
            import boto3

            client = boto3.client("sqs")
        self.client = client

    def queue_for(self, event: BaseModel) -> str:
        event_type = event.model_dump().get("event_type")
        if event_type == "JobCreated":
            if not self.job_created_queue_url:
                raise ValueError("AWS JobCreated queue URL is required")
            return self.job_created_queue_url
        if event_type == "VendorRecommendationGenerated":
            if not self.recommendation_queue_url:
                raise ValueError("AWS recommendation queue URL is required")
            return self.recommendation_queue_url
        if event_type in {
            "VendorRecommendationConfirmed",
            "VendorRecommendationOverridden",
        }:
            return self.override_queue_url or ""
        raise ValueError(f"Unsupported outbound event type: {event_type}")

    def publish(self, event: BaseModel) -> dict[str, Any]:
        record = event.model_dump(mode="json")
        queue_url = self.queue_for(event)
        if queue_url:
            self.client.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(record),
                MessageAttributes={
                    "event_type": {
                        "DataType": "String",
                        "StringValue": record["event_type"],
                    }
                }
            )
        return record

    def list_events(self) -> list[dict[str, Any]]:
        """The durable queue is not read by the API inspection endpoint."""

        return []


class AwsS3AuditLogger:
    """Write immutable, redacted audit records to Amazon S3.

    Free-text redaction is not configurable here: the durable, long-retention
    audit trail is exactly the place where retained free text becomes a data
    subject request problem, so it is always stripped.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "audit",
        client: Any | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("AUDIT_BUCKET is required")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.client = client
        self.records: list[dict[str, Any]] = []
        self._lock = Lock()

    def log(self, action: str, payload: BaseModel | dict[str, Any]) -> None:
        raw = (
            payload.model_dump(mode="json")
            if isinstance(payload, BaseModel)
            else payload
        )
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "payload": redact(raw, retain_free_text=False),
        }
        key = (
            f"{self.prefix}/{datetime.now(timezone.utc):%Y/%m/%d}/"
            f"{record['timestamp']}-{uuid4()}.json"
        )
        with self._lock:
            self.records.append(record)
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=json.dumps(record).encode("utf-8"),
                ContentType="application/json",
            )
