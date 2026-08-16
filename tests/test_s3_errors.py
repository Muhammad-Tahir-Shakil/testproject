"""Regression tests for S3 "missing key" interpretation.

Background: S3 answers GetObject on a key that does not exist with 403
AccessDenied -- not 404 NoSuchKey -- when the caller lacks s3:ListBucket on the
bucket. A store that only catches NoSuchKey therefore works against every key
it has already written and fails on the *first* read of a new key.

That shipped: the API Lambda held s3:GetObject on ``bucket/*`` but no
s3:ListBucket, so the first "Record decision" for any job returned
"Final decision state could not be loaded." The IAM grant is the fix; these
tests make the failure mode explicit and diagnosable if it ever regresses.
"""

import io

import pytest

from app.decisions import S3DecisionStore, create_decision
from app.models import OverrideRequest
from app.run_trace import AwsRunTraceStore, initial_trace
from app.s3_errors import S3PermissionError, is_missing_key


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


@pytest.fixture(autouse=True)
def fake_botocore(monkeypatch):
    import sys
    import types

    module = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = ClientError
    module.exceptions = exceptions
    monkeypatch.setitem(sys.modules, "botocore", module)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions)
    yield


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_absent_key_codes_are_reported_as_missing(code: str) -> None:
    assert is_missing_key(ClientError(code), bucket="b", key="k") is True


@pytest.mark.parametrize("code", ["403", "AccessDenied", "Forbidden"])
def test_access_denied_raises_an_actionable_error(code: str) -> None:
    """Never silently report a permission failure as an empty store."""

    with pytest.raises(S3PermissionError) as caught:
        is_missing_key(ClientError(code), bucket="audit", key="decisions/JOB-1.json")

    message = str(caught.value)
    assert "s3:ListBucket" in message
    assert "audit" in message and "decisions/JOB-1.json" in message


def test_unrelated_errors_are_not_treated_as_missing() -> None:
    assert is_missing_key(ClientError("SlowDown"), bucket="b", key="k") is False


class DeniedS3:
    """S3 without s3:ListBucket: a missing key reads back as AccessDenied."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get_object(self, **kwargs):
        key = kwargs["Key"]
        if key not in self.objects:
            raise ClientError("AccessDenied")
        return {"Body": io.BytesIO(self.objects[key])}

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = kwargs["Body"]


class ListableS3(DeniedS3):
    """S3 with s3:ListBucket: a missing key reads back as NoSuchKey."""

    def get_object(self, **kwargs):
        key = kwargs["Key"]
        if key not in self.objects:
            raise ClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[key])}


def test_first_decision_read_fails_loudly_without_list_bucket() -> None:
    store = S3DecisionStore("audit", client=DeniedS3())

    with pytest.raises(S3PermissionError):
        store.get("JOB-NEW")


def test_first_decision_read_returns_none_with_list_bucket() -> None:
    """The behaviour the deployed role must produce."""

    store = S3DecisionStore("audit", client=ListableS3())

    assert store.get("JOB-NEW") is None


def test_decision_round_trip_once_the_key_exists() -> None:
    store = S3DecisionStore("audit", client=ListableS3())
    _, snapshot = create_decision(
        OverrideRequest(
            job_id="JOB-1",
            vendor_id="V-001",
            reason="Confirmed the recommendation",
            actor_id="cognito-sub-1",
        ),
        previous_recommendation={
            "recommended_vendor_id": "V-001",
            "recommendations": [
                {"vendor_id": "V-001", "vendor_name": "Northstar", "rank": 1}
            ],
        },
        previous_snapshot=None,
        final_vendor_name="Northstar",
    )

    store.save(snapshot)

    loaded = store.get("JOB-1")
    assert loaded is not None
    assert loaded.active.final_vendor_id == "V-001"


def test_run_trace_store_uses_the_same_interpretation() -> None:
    denied = AwsRunTraceStore("audit", client=DeniedS3())
    listable = AwsRunTraceStore("audit", client=ListableS3())

    with pytest.raises(S3PermissionError):
        denied.get("run-missing")
    assert listable.get("run-missing") is None

    listable.save(initial_trace("run-1", "JOB-1"))
    assert listable.get("run-1") is not None
