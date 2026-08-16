"""Tests for the AWS override authorization path.

This is the security-critical branch: it decides whose name ends up on an
audited dispatch decision. It previously had zero coverage because no test ever
set DEPLOYMENT_TARGET=aws.
"""

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.run_trace import RunTrace, initial_trace

SAMPLE_PATH = Path(__file__).parents[1] / "data" / "sample.json"


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs):
        key = kwargs["Key"]
        if key not in self.objects:
            raise ClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[key])}

    def head_object(self, **kwargs):
        if kwargs["Key"] not in self.objects:
            raise ClientError("404")
        return {}


class FakeSqs:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def send_message(self, **kwargs):
        self.messages.append(kwargs)


@pytest.fixture(autouse=True)
def fake_botocore(monkeypatch):
    """Swap botocore.exceptions for a stub the fake clients can raise.

    boto3 is imported first and deliberately: it pulls in botocore internals at
    import time, so stubbing sys.modules before boto3 has loaded would break the
    real import. The adapters do `from botocore.exceptions import ClientError`
    inside the function body, so they pick up the stub at call time.
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


@pytest.fixture
def aws_client(monkeypatch):
    """Build the app in AWS mode with fake S3 and SQS clients."""

    monkeypatch.setenv("DEPLOYMENT_TARGET", "aws")
    monkeypatch.setenv("AUDIT_BUCKET", "audit-bucket")
    monkeypatch.setenv("JOB_CREATED_QUEUE_URL", "https://sqs.test/job-created")

    s3 = FakeS3()
    sqs = FakeSqs()
    monkeypatch.setattr(
        "boto3.client",
        lambda service, *args, **kwargs: s3 if service == "s3" else sqs,
    )

    app = create_app()
    return TestClient(app), app, s3


def store_trace(app, s3, *, job_id="JOB-1001", request_id="run-1") -> RunTrace:
    trace = initial_trace(request_id, job_id)
    trace.candidate_vendor_ids = ["V-001", "V-002"]
    trace.candidate_vendor_names = {"V-001": "Northstar", "V-002": "RapidFix"}
    trace.recommendation = {
        "recommended_vendor_id": "V-001",
        "recommended_vendor_name": "Northstar",
        "recommendations": [
            {"vendor_id": "V-001", "vendor_name": "Northstar", "rank": 1, "score": 88.0}
        ],
    }
    app.state.run_trace_store.save(trace)
    return trace


def override_payload(**overrides) -> dict:
    payload = {
        "job_id": "JOB-1001",
        "vendor_id": "V-002",
        "reason": "Customer requested the nearer vendor",
        "actor_id": "attacker-supplied",
        "request_id": "run-1",
    }
    payload.update(overrides)
    return payload


def cognito_scope(client: TestClient, sub: str = "cognito-sub-1"):
    """Inject the API Gateway JWT authorizer context Mangum would provide."""

    return {
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub}}}}
    }


def post_override(client: TestClient, payload: dict, sub: str | None):
    # TestClient cannot set scope["aws.event"] directly, so patch the route's
    # actor lookup the same way API Gateway would populate it.
    import app.main as main_module

    original = main_module.authenticated_actor_id
    main_module.authenticated_actor_id = lambda request: sub
    try:
        return client.post("/overrides", json=payload)
    finally:
        main_module.authenticated_actor_id = original


def test_override_without_a_cognito_subject_is_rejected(aws_client) -> None:
    client, app, s3 = aws_client
    store_trace(app, s3)

    response = post_override(client, override_payload(), sub=None)

    assert response.status_code == 401


def test_override_actor_comes_from_the_token_not_the_body(aws_client) -> None:
    """A caller must not be able to attribute a decision to someone else."""

    client, app, s3 = aws_client
    store_trace(app, s3)

    response = post_override(client, override_payload(), sub="cognito-sub-1")

    assert response.status_code == 200
    assert response.json()["actor_id"] == "cognito-sub-1"
    assert response.json()["actor_id"] != "attacker-supplied"


def test_override_for_an_unknown_run_is_rejected(aws_client) -> None:
    client, app, s3 = aws_client

    response = post_override(
        client, override_payload(request_id="missing"), sub="cognito-sub-1"
    )

    assert response.status_code == 404


def test_override_for_a_different_job_is_rejected(aws_client) -> None:
    client, app, s3 = aws_client
    store_trace(app, s3)

    response = post_override(
        client, override_payload(job_id="JOB-OTHER"), sub="cognito-sub-1"
    )

    assert response.status_code == 422


def test_override_vendor_outside_the_snapshot_is_rejected(aws_client) -> None:
    """The chosen vendor must have been part of the scored candidate pool."""

    client, app, s3 = aws_client
    store_trace(app, s3)

    response = post_override(
        client, override_payload(vendor_id="V-999"), sub="cognito-sub-1"
    )

    assert response.status_code == 422


def test_successful_override_updates_the_run_trace(aws_client) -> None:
    client, app, s3 = aws_client
    store_trace(app, s3)

    response = post_override(client, override_payload(), sub="cognito-sub-1")
    assert response.status_code == 200

    trace = app.state.run_trace_store.get("run-1")
    assert trace.decision_state == "human_overridden"
    assert trace.final_vendor_id == "V-002"
    assert trace.final_vendor_name == "RapidFix"
    human_step = next(step for step in trace.steps if step.name == "Human decision")
    assert human_step.status == "completed"


def test_health_is_reachable_and_reports_scoring_mode(aws_client) -> None:
    client, _, _ = aws_client

    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["scoring_mode"] == "rules-only"
    assert "margin_threshold" in body


def test_local_dashboard_routes_are_disabled_in_aws_mode(aws_client) -> None:
    client, _, _ = aws_client

    assert client.get("/api/dashboard").status_code == 404
    assert client.post("/api/setup").status_code == 404


def test_run_creation_enqueues_job_created_and_persists_a_trace(aws_client) -> None:
    client, app, _ = aws_client
    sample = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))

    response = client.post("/runs", json=sample)

    assert response.status_code == 200
    request_id = response.json()["request_id"]
    trace = app.state.run_trace_store.get(request_id)
    assert trace is not None
    assert trace.candidate_count == len(sample["vendors"])
    assert trace.eligible_candidate_count == 3
