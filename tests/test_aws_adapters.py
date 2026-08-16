"""Offline tests for AWS SQS and S3 adapters using fake clients."""

import json

import pytest
from pydantic import BaseModel

from app.events import AwsS3AuditLogger, AwsSqsEventBus
from app.models import (
    JobCreatedEvent,
    OverrideResponse,
    RecommendationResponse,
    VendorRecommendationGeneratedEvent,
    utc_now,
)


class FakeSqsClient:
    def __init__(self) -> None:
        self.messages = []

    def send_message(self, **kwargs) -> None:
        self.messages.append(kwargs)


class FakeS3Client:
    def __init__(self) -> None:
        self.objects = []

    def put_object(self, **kwargs) -> None:
        self.objects.append(kwargs)


class UnknownEvent(BaseModel):
    event_type: str = "Unknown"


def generated_event() -> VendorRecommendationGeneratedEvent:
    response = RecommendationResponse(
        request_id="request-1",
        job_id="JOB-1",
        model_version="rules-v1",
        status="manual_review_required",
        generated_at=utc_now(),
        recommendations=[],
    )
    return VendorRecommendationGeneratedEvent(payload=response)


def test_sqs_adapter_routes_generated_event() -> None:
    client = FakeSqsClient()
    bus = AwsSqsEventBus("recommendation-url", "override-url", client=client)

    record = bus.publish(generated_event())

    assert record["event_type"] == "VendorRecommendationGenerated"
    assert client.messages[0]["QueueUrl"] == "recommendation-url"
    assert client.messages[0]["MessageAttributes"]["event_type"]["StringValue"] == (
        "VendorRecommendationGenerated"
    )


def test_sqs_adapter_rejects_unknown_event_type() -> None:
    bus = AwsSqsEventBus("recommendation-url", "override-url", client=FakeSqsClient())

    with pytest.raises(ValueError, match="Unsupported outbound event type"):
        bus.queue_for(UnknownEvent())


def test_sqs_adapter_routes_job_created_to_its_own_queue() -> None:
    """The API Lambda publishes JobCreated; the worker publishes the result."""

    client = FakeSqsClient()
    bus = AwsSqsEventBus(job_created_queue_url="job-url", client=client)
    event = JobCreatedEvent.model_validate(
        {
            "job": {
                "job_id": "JOB-1",
                "job_type": "repair",
                "region": "north",
                "sla_hours": 8,
            },
            "vendors": [
                {
                    "vendor_id": "V-1",
                    "name": "Vendor One",
                    "capacity_total": 1,
                    "capacity_used": 0,
                    "completion_rate": 1,
                    "similar_job_rate": 1,
                    "avg_response_hours": 1,
                    "rework_rate": 0,
                }
            ],
        }
    )

    bus.publish(event)

    assert client.messages[0]["QueueUrl"] == "job-url"
    assert client.messages[0]["MessageAttributes"]["event_type"]["StringValue"] == (
        "JobCreated"
    )


def test_sqs_adapter_requires_the_queue_it_is_asked_to_use() -> None:
    bus = AwsSqsEventBus(recommendation_queue_url="rec-url", client=FakeSqsClient())

    with pytest.raises(ValueError, match="JobCreated queue URL"):
        bus.queue_for(
            JobCreatedEvent.model_validate(
                {
                    "job": {
                        "job_id": "JOB-1",
                        "job_type": "repair",
                        "region": "north",
                        "sla_hours": 8,
                    },
                    "vendors": [
                        {
                            "vendor_id": "V-1",
                            "name": "Vendor One",
                            "capacity_total": 1,
                            "capacity_used": 0,
                            "completion_rate": 1,
                            "similar_job_rate": 1,
                            "avg_response_hours": 1,
                            "rework_rate": 0,
                        }
                    ],
                }
            )
        )


def test_sqs_adapter_requires_at_least_one_queue_url() -> None:
    with pytest.raises(ValueError, match="At least one"):
        AwsSqsEventBus(client=FakeSqsClient())


def test_sqs_adapter_keeps_override_audit_only_without_extra_queue() -> None:
    client = FakeSqsClient()
    bus = AwsSqsEventBus("recommendation-url", client=client)
    override = OverrideResponse(
        event_id="override-1",
        job_id="JOB-1",
        vendor_id="V-1",
        reason="Manual dispatch decision",
        actor_id="dispatcher-1",
        recorded_at=utc_now(),
    )

    record = bus.publish(override)

    assert record["event_type"] == "VendorRecommendationOverridden"
    assert client.messages == []


def test_s3_audit_adapter_redacts_and_uploads() -> None:
    client = FakeS3Client()
    audit = AwsS3AuditLogger("audit-bucket", client=client)

    audit.log(
        "TestAction",
        {"customer_email": "person@example.com", "job_id": "JOB-1"},
    )

    uploaded = json.loads(client.objects[0]["Body"])
    assert client.objects[0]["Bucket"] == "audit-bucket"
    assert uploaded["payload"]["customer_email"] == "[REDACTED]"
    assert uploaded["payload"]["job_id"] == "JOB-1"


def test_s3_audit_keys_are_date_partitioned_and_unique() -> None:
    """Date partitioning keeps prefix listing usable as the trail grows."""

    client = FakeS3Client()
    audit = AwsS3AuditLogger("audit-bucket", client=client)

    audit.log("A", {"n": 1})
    audit.log("B", {"n": 2})

    keys = [item["Key"] for item in client.objects]
    assert len(set(keys)) == 2
    for key in keys:
        parts = key.split("/")
        assert parts[0] == "audit"
        assert len(parts[1]) == 4 and parts[1].isdigit()  # year
        assert len(parts[2]) == 2 and parts[2].isdigit()  # month
        assert len(parts[3]) == 2 and parts[3].isdigit()  # day
        assert key.endswith(".json")


def test_s3_audit_never_retains_free_text() -> None:
    """The durable trail is exactly where retained free text becomes a problem."""

    client = FakeS3Client()
    audit = AwsS3AuditLogger("audit-bucket", client=client)

    audit.log("TestAction", {"details": "Call Dave on 555-0100", "job_type": "repair"})

    uploaded = json.loads(client.objects[0]["Body"])
    assert uploaded["payload"]["details"] == "[REDACTED_FREE_TEXT]"
    assert uploaded["payload"]["job_type"] == "repair"


def test_s3_audit_requires_a_bucket() -> None:
    with pytest.raises(ValueError, match="AUDIT_BUCKET"):
        AwsS3AuditLogger("", client=FakeS3Client())
