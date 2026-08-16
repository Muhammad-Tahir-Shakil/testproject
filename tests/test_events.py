"""Audit sink tests, including the PII safety boundary.

The redaction path that actually runs in production walks a *list* of vendors
nested inside a request object. The earlier tests used only flat and nested
dicts with exact-case keys, so neither the list branch nor the case-insensitive
match was ever verified.
"""

import json

from app.events import (
    FREE_TEXT_KEYS,
    REDACTED,
    REDACTED_TEXT,
    SENSITIVE_KEYS,
    AuditLogger,
    InMemoryEventBus,
    redact,
)
from app.models import OverrideResponse, RecommendationRequest, utc_now


def test_redact_removes_common_pii_recursively() -> None:
    payload = {
        "job_id": "JOB-1",
        "customer_email": "customer@example.com",
        "nested": {"phone": "+1-555-0100"},
    }

    redacted = redact(payload)

    assert redacted == {
        "job_id": "JOB-1",
        "customer_email": REDACTED,
        "nested": {"phone": REDACTED},
    }


def test_redact_walks_lists() -> None:
    """The production payload nests a vendor list inside the request object."""

    payload = {
        "vendors": [
            {"vendor_id": "V-1", "email": "ops@v1.example"},
            {"vendor_id": "V-2", "email": "ops@v2.example"},
        ]
    }

    redacted = redact(payload)

    assert [item["vendor_id"] for item in redacted["vendors"]] == ["V-1", "V-2"]
    assert all(item["email"] == REDACTED for item in redacted["vendors"])


def test_redact_matches_keys_case_insensitively() -> None:
    redacted = redact({"Customer_Name": "Ada", "EMAIL": "ada@example.com"})

    assert redacted == {"Customer_Name": REDACTED, "EMAIL": REDACTED}


def test_free_text_is_removed_but_decision_evidence_is_kept() -> None:
    """A dispatcher can and does paste a phone number into `details`.

    The audit record stays explainable without it, because everything the
    scorer consumed -- job type, skills, factors, rationale -- is retained.
    """

    payload = {
        "job_id": "JOB-1",
        "job_type": "HVAC repair",
        "required_skills": ["hvac", "electrical"],
        "title": "Emergency chiller failure",
        "details": "Call Dave on 555-0100, gate code 4417",
        "asset_label": "Chiller CH-04",
        "site_name": "Pinecrest Mall",
        "customer_name": "Pinecrest Retail Group",
        "rationale": "Northstar ranked #1 due to 94% completion rate.",
    }

    redacted = redact(payload)

    assert redacted["title"] == REDACTED_TEXT
    assert redacted["details"] == REDACTED_TEXT
    assert redacted["asset_label"] == REDACTED_TEXT
    assert redacted["site_name"] == REDACTED
    assert redacted["customer_name"] == REDACTED
    assert redacted["job_type"] == "HVAC repair"
    assert redacted["required_skills"] == ["hvac", "electrical"]
    assert "94% completion rate" in redacted["rationale"]
    assert "555-0100" not in json.dumps(redacted)
    assert "Pinecrest" not in json.dumps(redacted)


def test_free_text_can_be_retained_for_local_debugging_only() -> None:
    payload = {"title": "Chiller down", "customer_name": "Ada"}

    retained = redact(payload, retain_free_text=True)

    assert retained["title"] == "Chiller down"
    # Direct identifiers are removed regardless of the flag.
    assert retained["customer_name"] == REDACTED


def test_empty_free_text_is_left_alone() -> None:
    assert redact({"title": ""})["title"] == ""


def test_sensitive_and_free_text_key_sets_do_not_overlap() -> None:
    assert not SENSITIVE_KEYS & FREE_TEXT_KEYS


def test_a_full_request_payload_is_redacted_end_to_end() -> None:
    """Exercise the real contract shape rather than a hand-made dict."""

    request = RecommendationRequest.model_validate(
        {
            "job": {
                "job_id": "JOB-1",
                "job_type": "HVAC repair",
                "customer_name": "Pinecrest Retail Group",
                "site_name": "Pinecrest Mall",
                "title": "Emergency chiller failure",
                "details": "Contact 555-0100",
                "region": "north",
                "sla_hours": 8,
            },
            "vendors": [
                {
                    "vendor_id": "V-1",
                    "name": "Northstar",
                    "capacity_total": 5,
                    "capacity_used": 1,
                    "completion_rate": 0.9,
                    "similar_job_rate": 0.9,
                    "avg_response_hours": 2,
                    "rework_rate": 0.1,
                }
            ],
        }
    )

    redacted = redact(request.model_dump(mode="json"))

    serialized = json.dumps(redacted)
    assert "Pinecrest" not in serialized
    assert "555-0100" not in serialized
    assert redacted["job"]["job_type"] == "HVAC repair"
    assert redacted["vendors"][0]["vendor_id"] == "V-1"


def test_audit_logger_writes_json_lines(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(str(path))

    logger.log(
        "TestAction",
        {"customer_email": "customer@example.com", "value": "kept"},
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["action"] == "TestAction"
    assert record["payload"]["customer_email"] == REDACTED
    assert record["payload"]["value"] == "kept"


def test_audit_logger_without_a_path_keeps_records_in_memory_only() -> None:
    logger = AuditLogger()

    logger.log("TestAction", {"value": "kept"})

    assert logger.path is None
    assert logger.records[0]["payload"]["value"] == "kept"


def test_audit_logger_appends_rather_than_truncating(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(str(path))

    logger.log("First", {"n": 1})
    logger.log("Second", {"n": 2})

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line)["action"] for line in lines] == ["First", "Second"]


def test_in_memory_bus_returns_a_copy_of_its_events() -> None:
    bus = InMemoryEventBus()
    bus.publish(
        OverrideResponse(
            event_id="e-1",
            job_id="JOB-1",
            vendor_id="V-1",
            reason="Because",
            actor_id="dispatcher-1",
            recorded_at=utc_now(),
        )
    )

    events = bus.list_events()
    events.clear()

    assert len(bus.list_events()) == 1
