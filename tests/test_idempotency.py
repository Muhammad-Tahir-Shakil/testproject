"""Tests for the S3 conditional-write idempotency claim."""

import pytest

from app.idempotency import IdempotencyStore, InMemoryIdempotencyStore


class ClientError(Exception):
    """Stand-in for botocore.exceptions.ClientError."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


@pytest.fixture(autouse=True)
def fake_botocore(monkeypatch):
    """Swap botocore.exceptions for a stub the fake clients can raise.

    IdempotencyStore imports ClientError inside the method body, so the stub is
    resolved at call time. boto3 is never constructed here because every test
    injects its own client.
    """

    import sys
    import types

    module = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = ClientError
    module.exceptions = exceptions
    monkeypatch.setitem(sys.modules, "botocore", module)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions)
    yield


class ConditionalS3:
    """Fake S3 that honours IfNoneMatch the way the real service does."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[dict] = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        key = kwargs["Key"]
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise ClientError("PreconditionFailed")
        self.objects[key] = kwargs["Body"]

    def head_object(self, **kwargs):
        if kwargs["Key"] not in self.objects:
            raise ClientError("404")
        return {}


class LegacyS3(ConditionalS3):
    """Fake S3 whose botocore is too old to know about IfNoneMatch."""

    def put_object(self, **kwargs):
        if "IfNoneMatch" in kwargs:
            raise ValueError(
                'Parameter validation failed: Unknown parameter "IfNoneMatch"'
            )
        return super().put_object(**kwargs)


def test_first_claim_wins_and_redelivery_is_rejected() -> None:
    client = ConditionalS3()
    store = IdempotencyStore("bucket", client=client)

    assert store.claim("evt-1", {"job_id": "JOB-1"}) is True
    assert store.claim("evt-1", {"job_id": "JOB-1"}) is False
    assert store.claim("evt-2") is True
    assert client.put_calls[0]["IfNoneMatch"] == "*"


def test_unrelated_client_errors_propagate() -> None:
    class BrokenS3(ConditionalS3):
        def put_object(self, **kwargs):
            raise ClientError("AccessDenied")

    store = IdempotencyStore("bucket", client=BrokenS3())

    with pytest.raises(ClientError):
        store.claim("evt-1")


def test_store_degrades_and_reports_when_conditional_writes_are_unavailable() -> None:
    """The weakened guarantee must be visible, not silent."""

    client = LegacyS3()
    store = IdempotencyStore("bucket", client=client)

    assert store.claim("evt-1") is True
    assert store.conditional_writes is False
    assert store.claim("evt-1") is False


def test_bucket_is_required() -> None:
    with pytest.raises(ValueError):
        IdempotencyStore("")


def test_in_memory_store_matches_the_contract() -> None:
    store = InMemoryIdempotencyStore()

    assert store.claim("evt-1") is True
    assert store.claim("evt-1") is False
    assert store.key("evt-1").endswith("evt-1.json")
