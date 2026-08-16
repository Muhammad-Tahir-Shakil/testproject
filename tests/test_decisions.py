"""Versioned confirmation, override, and decision-store tests."""

import io

from app.decisions import S3DecisionStore, create_decision
from app.models import DecisionSnapshot, OverrideRequest


def request(vendor_id: str, reason: str) -> OverrideRequest:
    return OverrideRequest(
        job_id="JOB-DECISION",
        vendor_id=vendor_id,
        reason=reason,
        actor_id="dispatcher-1",
        request_id="run-1",
    )


def recommendation() -> dict:
    return {
        "recommended_vendor_id": "V-001",
        "recommended_vendor_name": "Northstar Services",
        "recommendations": [
            {
                "vendor_id": "V-001",
                "vendor_name": "Northstar Services",
                "rank": 1,
                "score": 88.5,
            }
        ],
    }


def test_confirmation_is_idempotent_and_vendor_change_creates_revision() -> None:
    confirmed, snapshot = create_decision(
        request("V-001", "Confirmed the AI recommendation"),
        previous_recommendation=recommendation(),
        previous_snapshot=None,
        final_vendor_name="Northstar Services",
    )

    assert confirmed.decision_type == "confirmed"
    assert confirmed.decision_version == 1
    assert confirmed.changed is False

    repeated, same_snapshot = create_decision(
        request("V-001", "Repeated confirmation"),
        previous_recommendation=recommendation(),
        previous_snapshot=snapshot,
        final_vendor_name="Northstar Services",
    )
    assert repeated.idempotent is True
    assert repeated.decision_version == 1
    assert same_snapshot.revisions == snapshot.revisions

    revised, revised_snapshot = create_decision(
        request("V-002", "Customer requested a nearer vendor"),
        previous_recommendation=recommendation(),
        previous_snapshot=snapshot,
        final_vendor_name="RapidFix Partners",
    )
    assert revised.decision_type == "overridden"
    assert revised.decision_version == 2
    assert revised.previous_vendor_id == "V-001"
    assert revised.changed is True
    assert [item.decision_version for item in revised_snapshot.revisions] == [1, 2]


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, **kwargs):
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]

    def get_object(self, **kwargs):
        body = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        return {"Body": io.BytesIO(body)}


def test_s3_decision_store_round_trips_versioned_snapshot() -> None:
    store = S3DecisionStore("bucket", client=FakeS3())
    _, snapshot = create_decision(
        request("V-001", "Confirmed"),
        previous_recommendation=recommendation(),
        previous_snapshot=None,
        final_vendor_name="Northstar Services",
    )

    store.save(snapshot)
    loaded = store.get("JOB-DECISION")

    assert isinstance(loaded, DecisionSnapshot)
    assert loaded.active.final_vendor_id == "V-001"
    assert loaded.revisions[0].decision_version == 1


def test_s3_decision_store_recovers_from_legacy_document() -> None:
    client = FakeS3()
    store = S3DecisionStore("bucket", client=client)
    client.objects[("bucket", store.key("JOB-DECISION"))] = b'{"legacy": true}'

    assert store.get("JOB-DECISION") is None
