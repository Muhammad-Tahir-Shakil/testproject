"""Tests for structured logging and its PII boundary.

Logs are a data store with a retention policy, so they sit inside the same PII
boundary as the audit trail. These tests pin that.
"""

import json
import logging

from app.observability import JsonFormatter, configure_logging, get_logger


def record_for(**extra) -> str:
    logger = logging.getLogger("test.observability")
    formatter = JsonFormatter()
    record = logger.makeRecord(
        name="test.observability",
        level=logging.INFO,
        fn=__file__,
        lno=1,
        msg="recommendation.generated",
        args=(),
        exc_info=None,
        extra=extra,
    )
    return formatter.format(record)


def test_record_is_a_single_json_object_with_the_event_name() -> None:
    payload = json.loads(record_for(job_id="JOB-1"))

    assert payload["event"] == "recommendation.generated"
    assert payload["level"] == "INFO"
    assert payload["service"] == "vendor-dispatch"
    assert payload["job_id"] == "JOB-1"
    assert "timestamp" in payload


def test_structured_fields_are_preserved_for_querying() -> None:
    payload = json.loads(
        record_for(
            request_id="req-1",
            model_version="rules-v1",
            decision_state="manual_review_required",
            top_confidence=0.36,
            decision_margin=2.98,
        )
    )

    # These are exactly the fields the drift queries in answers.md rely on.
    assert payload["request_id"] == "req-1"
    assert payload["model_version"] == "rules-v1"
    assert payload["decision_state"] == "manual_review_required"
    assert payload["top_confidence"] == 0.36
    assert payload["decision_margin"] == 2.98


def test_customer_identifiers_and_free_text_never_reach_the_logs() -> None:
    payload = json.loads(
        record_for(
            job_id="JOB-1",
            customer_name="Pinecrest Retail Group",
            site_name="Pinecrest Mall",
            title="Emergency chiller failure",
            details="Call Dave on 555-0100, gate code 4417",
            email="dave@example.com",
        )
    )

    assert payload["job_id"] == "JOB-1"
    for banned in ("customer_name", "site_name", "title", "details", "email"):
        assert banned not in payload
    assert "555-0100" not in json.dumps(payload)
    assert "Pinecrest" not in json.dumps(payload)


def test_none_values_are_dropped_rather_than_logged_as_null() -> None:
    payload = json.loads(record_for(job_id="JOB-1", top_vendor_id=None))

    assert "top_vendor_id" not in payload


def test_exceptions_are_captured_under_an_error_key() -> None:
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.getLogger("test.observability").makeRecord(
            name="test.observability",
            level=logging.ERROR,
            fn=__file__,
            lno=1,
            msg="job_created.failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    payload = json.loads(formatter.format(record))

    assert payload["event"] == "job_created.failed"
    assert "boom" in payload["error"]


def test_configure_logging_reformats_an_existing_handler() -> None:
    """Lambda pre-installs a root handler; basicConfig alone is a no-op there."""

    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    try:
        root.handlers = [logging.StreamHandler()]
        configure_logging("DEBUG")
        assert root.level == logging.DEBUG
        assert all(
            isinstance(handler.formatter, JsonFormatter) for handler in root.handlers
        )
    finally:
        root.handlers = original_handlers
        root.setLevel(original_level)


def test_get_logger_returns_a_standard_logger() -> None:
    assert isinstance(get_logger("anything"), logging.Logger)
