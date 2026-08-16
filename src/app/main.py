"""FastAPI adapter for the vendor recommendation workflow."""

import os
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .decisions import S3DecisionStore, snapshot_from_response
from .events import AuditLogger, AuditOnlyEventBus, InMemoryEventBus
from .models import (
    JobCreatedEvent,
    OverrideRequest,
    OverrideResponse,
    RecommendationRequest,
    RecommendationResponse,
)
from .observability import configure_logging, get_logger
from .run_trace import RunAcceptedResponse, RunTrace, update_step
from .scoring import CONFIDENCE_THRESHOLD, MARGIN_THRESHOLD, MODEL_VERSION
from .service import RecommendationService


logger = get_logger(__name__)


def is_aws_mode() -> bool:
    return os.getenv("DEPLOYMENT_TARGET") == "aws"


def authenticated_actor_id(request: Request) -> str | None:
    """Read only the Cognito subject injected by API Gateway JWT auth."""

    event = request.scope.get("aws.event", {})
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    return claims.get("sub")


def create_app(
    *,
    service: RecommendationService | None = None,
    audit_path: str | None = None,
) -> FastAPI:
    """Create an isolated app for production and tests."""

    configure_logging()

    if service is None:
        model_version = os.getenv("MODEL_VERSION", MODEL_VERSION)
        confidence_threshold = float(
            os.getenv("CONFIDENCE_THRESHOLD", str(CONFIDENCE_THRESHOLD))
        )
        if is_aws_mode():
            from .events import AwsS3AuditLogger

            service = RecommendationService(
                event_bus=AuditOnlyEventBus(),
                audit_logger=AwsS3AuditLogger(bucket=os.environ["AUDIT_BUCKET"]),
                model_version=model_version,
                confidence_threshold=confidence_threshold,
            )
        else:
            service = RecommendationService(
                event_bus=InMemoryEventBus(),
                audit_logger=AuditLogger(
                    audit_path
                    or os.getenv(
                        "AUDIT_LOG_PATH",
                        str(_runtime_dir() / "audit.jsonl"),
                    )
                ),
                model_version=model_version,
                confidence_threshold=confidence_threshold,
            )

    app = FastAPI(
        title="RetailFixIt Vendor Recommendation Service",
        version="0.2.0",
        description="Explainable vendor ranking with event and override contracts.",
    )
    app.state.service = service
    app.state.local_workflow = None
    app.state.local_workflow_lock = Lock()

    if is_aws_mode():
        from .events import AwsSqsEventBus
        from .run_trace import AwsRunCoordinator, AwsRunTraceStore

        app.state.run_trace_store = AwsRunTraceStore(bucket=os.environ["AUDIT_BUCKET"])
        app.state.decision_store = S3DecisionStore(bucket=os.environ["AUDIT_BUCKET"])
        app.state.run_coordinator = AwsRunCoordinator(
            queue=AwsSqsEventBus(
                job_created_queue_url=os.environ["JOB_CREATED_QUEUE_URL"],
            ),
            trace_store=app.state.run_trace_store,
        )
    else:
        app.state.run_coordinator = None
        app.state.run_trace_store = None
        app.state.decision_store = None

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    def get_local_workflow():
        if is_aws_mode():
            raise HTTPException(
                status_code=404,
                detail="Local dashboard endpoints are disabled in AWS mode",
            )
        # Sync endpoints run in Starlette's threadpool, so two concurrent first
        # requests would otherwise both construct a LocalWorkflow and both
        # retrain the model.
        workflow = app.state.local_workflow
        if workflow is not None:
            return workflow
        with app.state.local_workflow_lock:
            if app.state.local_workflow is None:
                from .local_workflow import LocalWorkflow

                runtime_dir = _runtime_dir()
                app.state.local_workflow = LocalWorkflow(
                    db_path=runtime_dir / "dispatch.db",
                    model_path=runtime_dir / "local_model.json",
                )
            return app.state.local_workflow

    def require_local_operator(
        dispatcher_id: str | None,
        admin_token: str | None,
        *,
        privileged: bool = False,
    ) -> str:
        """Establish who is acting on the local dashboard.

        The AWS path derives the actor from the verified Cognito subject. The
        local path has no identity provider, so it does the next best thing:
        the actor comes from a request header rather than the request body, and
        an optional shared token gates destructive routes. This is not a
        substitute for authentication -- it makes local audit attribution
        deliberate rather than free-text, and stops a stray cross-origin POST
        from wiping local state.
        """

        expected = os.getenv("LOCAL_ADMIN_TOKEN")
        if expected and admin_token != expected:
            raise HTTPException(
                status_code=401,
                detail="A valid X-Local-Admin-Token header is required.",
            )
        if privileged and not expected:
            logger.warning(
                "local.privileged_route_unprotected",
                extra={"hint": "Set LOCAL_ADMIN_TOKEN to gate destructive routes."},
            )
        actor = (dispatcher_id or "").strip()
        if not actor:
            raise HTTPException(
                status_code=400,
                detail="An X-Dispatcher-Id header is required to attribute this action.",
            )
        return actor

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.options("/", include_in_schema=False)
    def options_root() -> Response:
        return Response(status_code=204)

    @app.options("/{path:path}", include_in_schema=False)
    def options_proxy(path: str) -> Response:
        return Response(status_code=204)

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Unauthenticated liveness and configuration probe.

        Exposes only non-secret configuration so an uptime monitor and the
        deployment smoke test can run without a Cognito token.
        """

        return {
            "status": "ok",
            "model_version": service.model_version,
            "confidence_threshold": service.confidence_threshold,
            "margin_threshold": MARGIN_THRESHOLD,
            "scoring_mode": "hybrid" if service.model_predictor else "rules-only",
        }

    @app.post("/recommendations", response_model=RecommendationResponse)
    def recommendations(request: RecommendationRequest) -> RecommendationResponse:
        return service.recommend(request)

    @app.post("/runs", response_model=RunAcceptedResponse)
    def create_run(request: RecommendationRequest) -> RunAcceptedResponse:
        if app.state.run_coordinator is None:
            raise HTTPException(status_code=404, detail="AWS run workflow is disabled")
        return app.state.run_coordinator.create(request)

    @app.get("/runs/{request_id}", response_model=RunTrace)
    def get_run(request_id: str) -> RunTrace:
        if app.state.run_trace_store is None:
            raise HTTPException(status_code=404, detail="AWS run workflow is disabled")
        trace = app.state.run_trace_store.get(request_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="Run trace not found")
        return trace

    @app.post("/events/job-created", response_model=RecommendationResponse)
    def job_created(event: JobCreatedEvent) -> RecommendationResponse:
        return service.handle_job_created(event)

    @app.get("/events")
    def events() -> list[dict[str, Any]]:
        return service.event_bus.list_events()

    @app.post("/overrides", response_model=OverrideResponse)
    def overrides(request: Request, payload: OverrideRequest) -> OverrideResponse:
        trace = None
        previous_recommendation = None
        previous_decision = None
        final_vendor_name = None
        if is_aws_mode():
            actor_id = authenticated_actor_id(request)
            if not actor_id:
                raise HTTPException(
                    status_code=401,
                    detail="Authenticated Cognito subject is required",
                )
            payload = payload.model_copy(update={"actor_id": actor_id})
            if payload.request_id:
                # Re-read immediately before mutating: the worker writes the
                # same key, so an in-memory copy could be stale.
                trace = app.state.run_trace_store.get(payload.request_id)
                if trace is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Recommendation run was not found",
                    )
                if trace.job_id != payload.job_id:
                    raise HTTPException(
                        status_code=422,
                        detail="Override job does not match the recommendation run",
                    )
                if (
                    trace.candidate_vendor_ids
                    and payload.vendor_id not in trace.candidate_vendor_ids
                ):
                    raise HTTPException(
                        status_code=422,
                        detail="Override vendor was not part of the job snapshot",
                    )
                previous_recommendation = trace.recommendation
                final_vendor_name = trace.candidate_vendor_names.get(payload.vendor_id)
            try:
                previous_decision = app.state.decision_store.get(payload.job_id)
            except Exception as error:
                logger.exception(
                    "decision.load_failed",
                    extra={"job_id": payload.job_id},
                )
                raise HTTPException(
                    status_code=503,
                    detail="Final decision state could not be loaded.",
                ) from error
        try:
            response = service.record_override(
                payload,
                previous_recommendation=previous_recommendation,
                previous_snapshot=previous_decision,
                final_vendor_name=final_vendor_name,
            )
            if is_aws_mode() and not response.idempotent:
                app.state.decision_store.save(snapshot_from_response(response))
            if trace is not None:
                trace.override = response.model_dump(mode="json")
                trace.decision = response.model_dump(mode="json")
                trace.decision_state = (
                    "ai_recommendation_confirmed"
                    if response.decision_type == "confirmed"
                    else "human_overridden"
                )
                trace.final_vendor_id = response.vendor_id
                trace.final_vendor_name = response.final_vendor_name
                update_step(
                    trace,
                    "Human decision",
                    "completed",
                    f"{response.decision_type} v{response.decision_version}: "
                    f"{response.final_vendor_name or response.vendor_id}",
                )
                app.state.run_trace_store.save(trace)
        except Exception as error:
            logger.exception(
                "decision.record_failed",
                extra={"job_id": payload.job_id, "vendor_id": payload.vendor_id},
            )
            raise HTTPException(
                status_code=503,
                detail="Final decision could not be persisted; no decision was recorded.",
            ) from error
        return response

    @app.get("/api/dashboard")
    def local_dashboard() -> dict[str, Any]:
        return get_local_workflow().dashboard()

    @app.get("/api/infrastructure")
    def infrastructure() -> dict[str, Any]:
        return {
            "mode": "local-first",
            "local": [
                "FastAPI browser dashboard",
                "SQLite local state",
                "Local JSON linear model artifact",
                "Local event bus",
            ],
            "aws_mapping": [
                "API Gateway + Lambda",
                "SQS + dead-letter queue",
                "S3 encrypted audit bucket",
                "Cognito user pool and JWT authorizer",
                "CloudWatch structured JSON logs",
            ],
            "security": [
                "Local runtime stays under the project runtime directory",
                "AWS API requires a Cognito JWT; /health is the only open route",
                "S3 public access is blocked and non-TLS requests are denied",
                "SQS uses server-side encryption",
                "Audit records redact customer identifiers and free text",
            ],
        }

    @app.post("/api/setup")
    def local_setup() -> dict[str, Any]:
        return get_local_workflow().setup()

    @app.post("/api/local/run-sample", response_model=RecommendationResponse)
    def local_run_sample() -> RecommendationResponse:
        workflow = get_local_workflow()
        return workflow.process(workflow.sample_event())

    @app.post("/api/local/job-created", response_model=RecommendationResponse)
    def local_job_created(event: JobCreatedEvent) -> RecommendationResponse:
        return get_local_workflow().process(event)

    @app.post("/api/local/override", response_model=OverrideResponse)
    def local_override(
        payload: OverrideRequest,
        x_dispatcher_id: str | None = Header(default=None),
        x_local_admin_token: str | None = Header(default=None),
    ) -> OverrideResponse:
        actor = require_local_operator(x_dispatcher_id, x_local_admin_token)
        # The body's actor_id is advisory only; attribution comes from the
        # header so an audit record cannot claim an arbitrary identity.
        return get_local_workflow().override(
            payload.model_copy(update={"actor_id": actor})
        )

    @app.post("/api/local/reset")
    def local_reset(
        x_dispatcher_id: str | None = Header(default=None),
        x_local_admin_token: str | None = Header(default=None),
        x_confirm_reset: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = require_local_operator(
            x_dispatcher_id, x_local_admin_token, privileged=True
        )
        if x_confirm_reset != "reset":
            raise HTTPException(
                status_code=428,
                detail="Send X-Confirm-Reset: reset to delete local state.",
            )
        workflow = get_local_workflow()
        workflow.reset()
        with app.state.local_workflow_lock:
            app.state.local_workflow = None
        logger.warning("local.state_reset", extra={"actor_id": actor})
        return {"reset": True, "actor_id": actor}

    return app


def _runtime_dir() -> Path:
    """Resolve the writable runtime directory.

    Defaults under the project's gitignored ``runtime/`` rather than the
    process working directory, so a stray audit file cannot land wherever
    uvicorn happened to be started from.
    """

    # src-layout: this module is src/app/main.py, so runtime/ is two levels up.
    return Path(os.getenv("RUNTIME_DIR", str(Path(__file__).parents[2] / "runtime")))


app = create_app()
