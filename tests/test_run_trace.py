"""Tests for the live browser-to-AWS run trace contract."""

from app.models import RecommendationRequest
from app.run_trace import AwsRunCoordinator, RunTrace, initial_trace, update_step


class FakeQueue:
    def __init__(self) -> None:
        self.events = []

    def publish(self, event):
        self.events.append(event)
        return event.model_dump(mode="json")


class FakeTraceStore:
    def __init__(self) -> None:
        self.traces = {}

    def save(self, trace: RunTrace) -> None:
        self.traces[trace.request_id] = trace

    def get(self, request_id: str):
        return self.traces.get(request_id)


def request() -> RecommendationRequest:
    return RecommendationRequest.model_validate(
        {
            "job": {
                "job_id": "JOB-TRACE",
                "job_type": "repair",
                "region": "north",
                "sla_hours": 8,
                "risk_level": "low",
            },
            "vendors": [
                {
                    "vendor_id": "V-TRACE",
                    "name": "Trace Vendor",
                    "service_regions": ["north"],
                    "skills": ["repair"],
                    "capacity_total": 2,
                    "capacity_used": 0,
                    "completion_rate": 0.9,
                    "similar_job_rate": 0.8,
                    "avg_response_hours": 2,
                    "rework_rate": 0.1,
                }
            ],
        }
    )


def test_trace_steps_update_with_timestamps() -> None:
    trace = initial_trace("run-1", "JOB-TRACE")

    updated = update_step(trace, "SQS JobCreated", "running")
    updated = update_step(updated, "SQS JobCreated", "completed", "published")

    step = next(item for item in updated.steps if item.name == "SQS JobCreated")
    assert step.status == "completed"
    assert step.started_at is not None
    assert step.completed_at is not None


def test_coordinator_publishes_job_and_persists_trace() -> None:
    queue = FakeQueue()
    store = FakeTraceStore()
    coordinator = AwsRunCoordinator(queue, store)

    accepted = coordinator.create(request())

    assert accepted.status == "accepted"
    assert len(queue.events) == 1
    assert queue.events[0].event_id == accepted.request_id
    trace = store.get(accepted.request_id)
    assert trace is not None
    assert trace.status == "running"
    assert trace.candidate_count == 1
    assert trace.eligible_candidate_count == 1
    assert trace.candidate_vendor_names["V-TRACE"] == "Trace Vendor"
    assert next(
        step for step in trace.steps if step.name == "SQS JobCreated"
    ).status == "completed"
