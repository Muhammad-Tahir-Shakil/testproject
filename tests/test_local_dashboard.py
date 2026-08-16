"""Local SQLite, ML, workflow, and dashboard tests."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.local_workflow import LocalWorkflow
from app.main import create_app


def test_local_setup_trains_model_and_persists_workflow(tmp_path: Path) -> None:
    workflow = LocalWorkflow(tmp_path / "state.db", tmp_path / "model.json")

    setup = workflow.setup()
    response = workflow.process(workflow.sample_event())
    dashboard = workflow.dashboard()

    assert setup["ready"] is True
    assert setup["model"]["version"] == "hybrid-local-v1"
    assert setup["model"]["training_data_kind"] == "synthetic-fixture"
    assert response.status == "recommendations_ready"
    assert dashboard["setup_ready"] is True
    assert dashboard["counts"]["jobs"] == 1
    assert dashboard["counts"]["vendors"] == 4
    assert dashboard["counts"]["recommendations"] == 1
    assert dashboard["counts"]["events"] >= 2
    assert dashboard["counts"]["audit"] >= 4
    # The local workflow blends the trained model, so every recommendation
    # should carry both components rather than the rules score alone.
    assert response.recommendations[0].model_score is not None
    assert response.recommendations[0].rule_score > 0


def test_dashboard_counts_are_totals_not_page_lengths(tmp_path: Path) -> None:
    """The metric cards previously reported len() of a LIMIT-ed page."""

    workflow = LocalWorkflow(tmp_path / "state.db", tmp_path / "model.json")
    workflow.setup()
    for _ in range(7):
        workflow.process(workflow.sample_event())

    dashboard = workflow.dashboard()

    assert dashboard["counts"]["recommendations"] == 7
    assert len(dashboard["recommendations"]) == dashboard["page_sizes"][
        "recommendations"
    ]
    assert dashboard["counts"]["audit"] > len(dashboard["audit"])


def test_a_corrupt_model_artifact_does_not_break_the_dashboard(
    tmp_path: Path,
) -> None:
    """Recovery must not require deleting a file by hand."""

    artifact = tmp_path / "model.json"
    artifact.write_text("{ not json", encoding="utf-8")

    workflow = LocalWorkflow(tmp_path / "state.db", artifact)

    assert workflow.model.ready is False
    assert workflow.dashboard()["model"]["load_error"] is not None
    # Re-running setup retrains and clears the error.
    workflow.setup()
    assert workflow.model.ready is True
    assert workflow.dashboard()["model"]["load_error"] is None


def test_local_audit_applies_the_same_redaction_as_the_aws_sink(
    tmp_path: Path,
) -> None:
    workflow = LocalWorkflow(tmp_path / "state.db", tmp_path / "model.json")
    workflow.setup()
    workflow.process(workflow.sample_event())

    import json as _json

    serialized = _json.dumps(workflow.dashboard()["audit"])

    # data/sample.json uses this customer and site.
    assert "Pinecrest Retail Group" not in serialized
    assert "[REDACTED]" in serialized


def test_dashboard_setup_run_and_override_endpoints(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    client = TestClient(create_app())

    assert client.get("/").status_code == 200
    assert client.get("/api/dashboard").status_code == 200

    setup = client.post("/api/setup")
    assert setup.status_code == 200
    assert setup.json()["model"]["version"] == "hybrid-local-v1"

    recommendation = client.post("/api/local/run-sample")
    assert recommendation.status_code == 200
    assert recommendation.json()["status"] == "recommendations_ready"
    request_id = recommendation.json()["request_id"]
    assert recommendation.json()["candidate_count"] == 4
    assert recommendation.json()["eligible_candidate_count"] == 3

    confirmation = client.post(
        "/api/local/override",
        json={
            "job_id": "JOB-1001",
            "vendor_id": "V-001",
            "reason": "Dispatcher confirmed the highest-ranked eligible vendor",
            "actor_id": "dispatcher-local",
            "request_id": request_id,
        },
        headers={"X-Dispatcher-Id": "dispatcher-local"},
    )
    assert confirmation.status_code == 200
    assert confirmation.json()["decision_type"] == "confirmed"
    assert confirmation.json()["event_type"] == "VendorRecommendationConfirmed"
    assert confirmation.json()["decision_version"] == 1
    assert confirmation.json()["changed"] is False

    duplicate_confirmation = client.post(
        "/api/local/override",
        json={
            "job_id": "JOB-1001",
            "vendor_id": "V-001",
            "reason": "Repeated confirmation",
            "actor_id": "dispatcher-local",
            "request_id": request_id,
        },
        headers={"X-Dispatcher-Id": "dispatcher-local"},
    )
    assert duplicate_confirmation.status_code == 200
    assert duplicate_confirmation.json()["idempotent"] is True
    assert duplicate_confirmation.json()["decision_version"] == 1

    override = client.post(
        "/api/local/override",
        json={
            "job_id": "JOB-1001",
            "vendor_id": "V-002",
            "reason": "Dispatcher selected a nearer vendor",
            "actor_id": "dispatcher-local",
            "request_id": request_id,
        },
        headers={"X-Dispatcher-Id": "dispatcher-local"},
    )
    assert override.status_code == 200
    assert override.json()["decision_type"] == "overridden"
    assert override.json()["decision_version"] == 2
    assert override.json()["previous_vendor_id"] == "V-001"
    assert override.json()["final_vendor_name"] == "RapidFix Partners"
    assert override.json()["changed"] is True

    dashboard = client.get("/api/dashboard").json()
    assert dashboard["counts"]["audit"] >= 6
    assert "VendorRecommendationConfirmed" in {
        item["action"] for item in dashboard["audit"]
    }
    assert dashboard["decisions"][0]["active"]["final_vendor_id"] == "V-002"
    assert len(dashboard["decisions"][0]["revisions"]) == 2


def test_dashboard_reset_removes_local_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    client = TestClient(create_app())
    client.post("/api/setup")
    assert (tmp_path / "dispatch.db").exists()

    response = client.post(
        "/api/local/reset",
        headers={
            "X-Dispatcher-Id": "dispatcher-local",
            "X-Confirm-Reset": "reset",
        },
    )

    assert response.status_code == 200
    assert not (tmp_path / "dispatch.db").exists()
