"""Offline tests for the AWS Lambda SQS worker.

The earlier suite never set AUDIT_BUCKET, so the trace and idempotency branches
were entirely unexercised -- which is exactly why the messageId/event_id trace
bug survived. These tests run those branches with fake AWS clients.
"""

import io
import json
from pathlib import Path

import pytest

import lambda_function
from app.models import JobEvent, RecommendationResponse, ScoreFactors, utc_now
from app.run_trace import AwsRunTraceStore, initial_trace


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


@pytest.fixture(autouse=True)
def fake_botocore(monkeypatch):
    """Swap botocore.exceptions for a stub the fake clients can raise.

    boto3 is imported first and deliberately: it pulls in botocore internals at
    import time, so stubbing sys.modules before boto3 has loaded would break the
    real import.
    """

    import sys
    import types

    import boto3  # noqa: F401  -- ensure the real package is loaded first

    module = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = ClientError
    module.exceptions = exceptions
    monkeypatch.setitem(sys.modules, "botocore", module)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions)
    yield


CACHED = (
    "build_service",
    "build_trace_store",
    "build_idempotency_store",
    "build_model_predictor",
)


@pytest.fixture(autouse=True)
def clear_lambda_caches():
    """The handler memoises its AWS clients across warm invocations."""

    def clear():
        for name in CACHED:
            getattr(lambda_function, name).cache_clear()

    clear()
    yield
    clear()


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs):
        key = kwargs["Key"]
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise ClientError("PreconditionFailed")
        self.objects[key] = kwargs["Body"]

    def get_object(self, **kwargs):
        key = kwargs["Key"]
        if key not in self.objects:
            raise ClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[key])}


class FakeService:
    def __init__(self) -> None:
        self.events: list[JobEvent] = []

    def handle_job_created(self, event: JobEvent) -> RecommendationResponse:
        self.events.append(event)
        return RecommendationResponse(
            request_id="req-1",
            job_id=event.job.job_id,
            model_version="rules-v1",
            status="recommendations_ready",
            generated_at=utc_now(),
            recommendations=[],
            decision_state="ai_recommended",
            recommended_vendor_id="V-1",
            recommended_vendor_name="Vendor One",
        )


def valid_job_event(event_id: str = "evt-1") -> dict:
    return {
        "event_id": event_id,
        "event_type": "JobCreated",
        "job": {
            "job_id": "JOB-1",
            "job_type": "repair",
            "region": "north",
            "sla_hours": 8,
            "risk_level": "low",
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


def test_lambda_returns_partial_batch_failures(monkeypatch) -> None:
    fake_service = FakeService()
    monkeypatch.setattr(lambda_function, "build_service", lambda: fake_service)
    monkeypatch.delenv("AUDIT_BUCKET", raising=False)
    event = {
        "Records": [
            {"messageId": "good", "body": json.dumps(valid_job_event())},
            {"messageId": "bad", "body": '{"not": "a JobEvent"}'},
        ]
    }

    result = lambda_function.handler(event, None)

    assert [item.event_id for item in fake_service.events] == ["evt-1"]
    assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


def test_redelivered_message_is_skipped(monkeypatch) -> None:
    """SQS is at-least-once; a duplicate must not double-publish."""

    fake_service = FakeService()
    s3 = FakeS3()
    monkeypatch.setattr(lambda_function, "build_service", lambda: fake_service)
    monkeypatch.setenv("AUDIT_BUCKET", "audit-bucket")
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: s3)

    record = {"messageId": "m-1", "body": json.dumps(valid_job_event())}
    lambda_function.handler({"Records": [record]}, None)
    lambda_function.handler({"Records": [record]}, None)

    assert len(fake_service.events) == 1


def test_distinct_events_are_both_processed(monkeypatch) -> None:
    fake_service = FakeService()
    s3 = FakeS3()
    monkeypatch.setattr(lambda_function, "build_service", lambda: fake_service)
    monkeypatch.setenv("AUDIT_BUCKET", "audit-bucket")
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: s3)

    lambda_function.handler(
        {
            "Records": [
                {"messageId": "m-1", "body": json.dumps(valid_job_event("evt-1"))},
                {"messageId": "m-2", "body": json.dumps(valid_job_event("evt-2"))},
            ]
        },
        None,
    )

    assert [item.event_id for item in fake_service.events] == ["evt-1", "evt-2"]


def test_successful_run_completes_the_trace(monkeypatch) -> None:
    fake_service = FakeService()
    s3 = FakeS3()
    monkeypatch.setattr(lambda_function, "build_service", lambda: fake_service)
    monkeypatch.setenv("AUDIT_BUCKET", "audit-bucket")
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: s3)

    store = AwsRunTraceStore("audit-bucket", client=s3)
    store.save(initial_trace("evt-1", "JOB-1"))

    lambda_function.handler(
        {"Records": [{"messageId": "m-1", "body": json.dumps(valid_job_event())}]},
        None,
    )

    trace = store.get("evt-1")
    assert trace.status == "completed"
    assert trace.final_vendor_id == "V-1"
    statuses = {step.name: step.status for step in trace.steps}
    assert statuses["Worker Lambda"] == "completed"
    assert statuses["S3 audit"] == "completed"


def test_failed_run_marks_the_trace_failed(monkeypatch) -> None:
    """Regression test for the messageId/event_id trace-key bug.

    The trace is keyed by the JobCreated event_id. The failure handler used to
    look it up by the SQS messageId, so the lookup always missed, the failure
    was never recorded, and the browser polled a trace stuck at 'running' until
    it timed out -- showing a generic timeout for a specific, diagnosable error.
    """

    class ExplodingService:
        def handle_job_created(self, event):
            raise RuntimeError("scoring blew up")

    s3 = FakeS3()
    monkeypatch.setattr(lambda_function, "build_service", lambda: ExplodingService())
    monkeypatch.setenv("AUDIT_BUCKET", "audit-bucket")
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: s3)

    store = AwsRunTraceStore("audit-bucket", client=s3)
    store.save(initial_trace("evt-1", "JOB-1"))

    result = lambda_function.handler(
        {
            "Records": [
                {
                    "messageId": "sqs-message-id-differs",
                    "body": json.dumps(valid_job_event()),
                }
            ]
        },
        None,
    )

    assert result == {
        "batchItemFailures": [{"itemIdentifier": "sqs-message-id-differs"}]
    }
    trace = store.get("evt-1")
    assert trace.status == "failed"
    assert trace.error
    worker_step = next(step for step in trace.steps if step.name == "Worker Lambda")
    assert worker_step.status == "failed"


def test_unparseable_body_fails_without_raising(monkeypatch) -> None:
    """No event_id can be recovered, so there is no trace to update."""

    s3 = FakeS3()
    monkeypatch.setenv("AUDIT_BUCKET", "audit-bucket")
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: s3)

    result = lambda_function.handler(
        {"Records": [{"messageId": "m-1", "body": "not json at all"}]}, None
    )

    assert result == {"batchItemFailures": [{"itemIdentifier": "m-1"}]}


def test_model_predictor_is_absent_by_default(monkeypatch) -> None:
    """The deployed stack runs the rules baseline unless explicitly configured."""

    monkeypatch.delenv("SCORING_MODEL_ARTIFACT", raising=False)

    assert lambda_function.build_model_predictor() is None


def test_a_bad_model_artifact_degrades_to_rules(monkeypatch, tmp_path) -> None:
    """A broken artifact must never take the worker down."""

    bad = tmp_path / "model.json"
    bad.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("SCORING_MODEL_ARTIFACT", str(bad))

    assert lambda_function.build_model_predictor() is None


def test_a_valid_model_artifact_is_loaded(monkeypatch, tmp_path) -> None:
    from app.local_ml import LocalModel

    artifact = tmp_path / "model.json"
    LocalModel(artifact).train(Path(__file__).parents[1] / "data" / "training.json")
    monkeypatch.setenv("SCORING_MODEL_ARTIFACT", str(artifact))

    predictor = lambda_function.build_model_predictor()

    assert predictor is not None
    value = predictor(
        ScoreFactors(**{name: 0.5 for name in ScoreFactors.model_fields})
    )
    assert 0.0 <= value <= 1.0
