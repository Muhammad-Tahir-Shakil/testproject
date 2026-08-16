"""API, event, override, validation, and audit tests."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


SAMPLE_PATH = Path(__file__).parents[1] / "data" / "sample.json"


def sample_payload() -> dict:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


def client_for(tmp_path: Path) -> TestClient:
    return TestClient(create_app(audit_path=str(tmp_path / "audit.jsonl")))


def test_health_exposes_model_configuration(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["model_version"] == "rules-v1"
    assert body["scoring_mode"] == "rules-only"
    assert body["confidence_threshold"] > 0
    assert body["margin_threshold"] > 0


def test_health_leaks_no_customer_or_infrastructure_detail(tmp_path: Path) -> None:
    """/health is deliberately unauthenticated, so its body is a security surface."""

    body = client_for(tmp_path).get("/health").json()

    assert set(body) == {
        "status",
        "model_version",
        "confidence_threshold",
        "margin_threshold",
        "scoring_mode",
    }


def test_options_preflight_returns_success(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    response = client.options("/health")

    assert response.status_code == 204


def test_recommendation_endpoint_returns_contract_and_audit_records(
    tmp_path: Path,
) -> None:
    client = client_for(tmp_path)

    response = client.post("/recommendations", json=sample_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["event_type"] == "VendorRecommendationGenerated"
    assert body["status"] == "recommendations_ready"
    assert len(body["recommendations"]) == 3
    assert body["recommendations"][0]["rationale"]
    assert body["candidate_count"] == 4
    assert body["eligible_candidate_count"] == 3
    assert body["excluded_candidate_count"] == 1
    assert body["decision_state"] == "ai_recommended"
    assert body["recommended_vendor_id"] == "V-001"
    assert body["review_reasons"] == []
    assert body["decision_margin"] > 0

    audit_lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["action"] for line in audit_lines] == [
        "RecommendationInput",
        "RecommendationOutput",
    ]


def test_audit_records_keep_the_evidence_and_drop_the_pii(tmp_path: Path) -> None:
    """The written record must still explain the decision without identifiers."""

    client = client_for(tmp_path)
    client.post("/recommendations", json=sample_payload())

    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "Pinecrest Retail Group" not in raw
    assert "[REDACTED]" in raw

    output = json.loads(raw.splitlines()[1])["payload"]
    top = output["recommendations"][0]
    assert top["rationale"]
    assert top["score_factors"]
    assert output["model_version"] == "rules-v1"


def test_high_risk_job_never_reports_itself_as_ready(tmp_path: Path) -> None:
    """Policy, not score: the top candidate here is strong and still blocked."""

    client = client_for(tmp_path)
    payload = sample_payload()
    payload["job"]["risk_level"] = "high"

    body = client.post("/recommendations", json=payload).json()

    assert body["status"] == "manual_review_required"
    assert body["decision_state"] == "manual_review_required"
    assert body["recommendations"][0]["abstained"] is False
    assert any("high-risk" in reason for reason in body["review_reasons"])


def test_thin_evidence_forces_manual_review(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    payload = sample_payload()
    for vendor in payload["vendors"]:
        vendor["sample_size"] = 1
        vendor["data_age_hours"] = 2000

    body = client.post("/recommendations", json=payload).json()

    assert body["status"] == "manual_review_required"
    assert body["recommendations"][0]["abstained"] is True
    assert any("evidence" in reason for reason in body["review_reasons"])


def test_recommendation_endpoint_accepts_rich_customer_job_context(
    tmp_path: Path,
) -> None:
    client = client_for(tmp_path)
    payload = sample_payload()
    payload["job"].update(
        {
            "customer_name": "HarborPoint Property Trust",
            "site_name": "HarborPoint Tower · East Lobby",
            "asset_label": "Passenger lift L-02",
            "title": "Passenger elevator stopped between floors",
            "details": "The lift is showing a door interlock fault and needs a controls inspection.",
            "required_skills": [],
        }
    )

    response = client.post("/recommendations", json=payload)

    assert response.status_code == 200
    assert response.json()["recommendations"]
    assert "title/details" in response.json()["recommendations"][0]["rationale"]


def test_job_created_publishes_recommendation_event(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    payload = sample_payload()
    event_payload = {
        "event_id": "evt-1",
        "event_type": "JobCreated",
        "job": payload["job"],
        "vendors": payload["vendors"],
        "top_k": payload["top_k"],
    }

    response = client.post("/events/job-created", json=event_payload)

    assert response.status_code == 200
    events = client.get("/events").json()
    assert len(events) == 1
    assert events[0]["event_type"] == "VendorRecommendationGenerated"
    assert events[0]["payload"]["job_id"] == "JOB-1001"


def test_override_is_published_and_audited(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    response = client.post(
        "/overrides",
        json={
            "job_id": "JOB-1001",
            "vendor_id": "V-002",
            "reason": "Customer requested the nearer vendor",
            "actor_id": "dispatcher-7",
        },
    )

    assert response.status_code == 200
    assert response.json()["event_type"] == "VendorRecommendationOverridden"
    assert client.get("/events").json()[0]["vendor_id"] == "V-002"
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "VendorRecommendationOverridden" in audit


def test_invalid_request_is_rejected(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    response = client.post(
        "/recommendations",
        json={"job": sample_payload()["job"], "vendors": []},
    )

    assert response.status_code == 422


def test_no_eligible_vendor_requires_manual_review(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    payload = sample_payload()
    for vendor in payload["vendors"]:
        vendor["active"] = False

    response = client.post("/recommendations", json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "manual_review_required"
    assert response.json()["recommendations"] == []
