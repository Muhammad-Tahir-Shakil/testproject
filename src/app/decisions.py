"""Versioned final-decision state for local and S3-backed workflows.

S3 object versioning preserves the decision document and audit history. It is
not a transactional compare-and-set store, so concurrent AWS writes remain a
documented limitation of the S3-only deployment.
"""

import json
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from pydantic import ValidationError

from .models import (
    DecisionRevision,
    DecisionSnapshot,
    OverrideRequest,
    OverrideResponse,
)
from .s3_errors import is_missing_key


class DecisionStore(Protocol):
    def get(self, job_id: str) -> DecisionSnapshot | None:
        ...

    def save(self, snapshot: DecisionSnapshot) -> None:
        ...


def create_decision(
    request: OverrideRequest,
    *,
    previous_recommendation: dict[str, Any] | None,
    previous_snapshot: DecisionSnapshot | None,
    final_vendor_name: str | None,
) -> tuple[OverrideResponse, DecisionSnapshot]:
    """Build a confirmed/overridden decision and its active revision.

    A repeated submission for the active vendor is returned as an idempotent
    response and does not create a new revision.
    """

    recommendations = (previous_recommendation or {}).get("recommendations", [])
    ai_recommendation = recommendations[0] if recommendations else None
    ai_vendor_id = (
        (previous_recommendation or {}).get("recommended_vendor_id")
        or (ai_recommendation or {}).get("vendor_id")
    )
    ai_vendor_name = (
        (previous_recommendation or {}).get("recommended_vendor_name")
        or (ai_recommendation or {}).get("vendor_name")
    )
    previous_active = previous_snapshot.active if previous_snapshot else None

    if previous_active and previous_active.final_vendor_id == request.vendor_id:
        existing = previous_active
        response = OverrideResponse(
            event_id=str(uuid4()),
            event_type=(
                "VendorRecommendationConfirmed"
                if existing.decision_type == "confirmed"
                else "VendorRecommendationOverridden"
            ),
            job_id=request.job_id,
            vendor_id=existing.final_vendor_id,
            reason=existing.reason,
            actor_id=existing.actor_id,
            recorded_at=existing.recorded_at,
            request_id=existing.request_id or request.request_id,
            decision_id=existing.decision_id,
            decision_version=existing.decision_version,
            decision_type=existing.decision_type,
            idempotent=True,
            ai_vendor_id=existing.ai_vendor_id,
            ai_vendor_name=existing.ai_vendor_name,
            final_vendor_name=existing.final_vendor_name,
            changed=False,
            revision_history=previous_snapshot.revisions,
        )
        return response, previous_snapshot

    version = (previous_active.decision_version if previous_active else 0) + 1
    decision_type = "confirmed" if request.vendor_id == ai_vendor_id else "overridden"
    revision = DecisionRevision(
        decision_id=str(uuid4()),
        job_id=request.job_id,
        request_id=request.request_id,
        decision_version=version,
        decision_type=decision_type,
        ai_vendor_id=ai_vendor_id,
        ai_vendor_name=ai_vendor_name,
        final_vendor_id=request.vendor_id,
        final_vendor_name=final_vendor_name or request.vendor_id,
        reason=request.reason,
        actor_id=request.actor_id,
        recorded_at=datetime.now(timezone.utc),
    )
    revisions = [*(previous_snapshot.revisions if previous_snapshot else []), revision]
    snapshot = DecisionSnapshot(
        job_id=request.job_id,
        active=revision,
        revisions=revisions,
        updated_at=revision.recorded_at,
    )
    response = OverrideResponse(
        event_id=str(uuid4()),
        event_type=(
            "VendorRecommendationConfirmed"
            if decision_type == "confirmed"
            else "VendorRecommendationOverridden"
        ),
        job_id=request.job_id,
        vendor_id=request.vendor_id,
        reason=request.reason,
        actor_id=request.actor_id,
        recorded_at=revision.recorded_at,
        request_id=request.request_id,
        decision_id=revision.decision_id,
        decision_version=version,
        decision_type=decision_type,
        ai_vendor_id=ai_vendor_id,
        ai_vendor_name=ai_vendor_name,
        previous_vendor_id=(
            previous_active.final_vendor_id if previous_active else ai_vendor_id
        ),
        previous_vendor_name=(
            previous_active.final_vendor_name if previous_active else ai_vendor_name
        ),
        previous_rank=ai_recommendation.get("rank") if ai_recommendation else None,
        previous_score=(
            ai_recommendation.get("score") if ai_recommendation else None
        ),
        final_vendor_name=revision.final_vendor_name,
        changed=bool(
            previous_active
            and previous_active.final_vendor_id != request.vendor_id
        ),
        revision_history=revisions,
    )
    return response, snapshot


def snapshot_from_response(response: OverrideResponse) -> DecisionSnapshot:
    """Convert an API decision response into persisted active state."""

    active = DecisionRevision(
        decision_id=response.decision_id,
        job_id=response.job_id,
        request_id=response.request_id,
        decision_version=response.decision_version,
        decision_type=response.decision_type,
        ai_vendor_id=response.ai_vendor_id,
        ai_vendor_name=response.ai_vendor_name,
        final_vendor_id=response.vendor_id,
        final_vendor_name=response.final_vendor_name,
        reason=response.reason,
        actor_id=response.actor_id,
        recorded_at=response.recorded_at,
    )
    revisions = response.revision_history or [active]
    return DecisionSnapshot(
        job_id=response.job_id,
        active=active,
        revisions=revisions,
        updated_at=response.recorded_at,
    )


class S3DecisionStore:
    """Best-effort active decision store using versioned S3 JSON objects."""

    def __init__(self, bucket: str, client: Any | None = None) -> None:
        if not bucket:
            raise ValueError("AUDIT_BUCKET is required")
        self.bucket = bucket
        self.prefix = "decisions"
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.client = client

    def key(self, job_id: str) -> str:
        return f"{self.prefix}/{job_id}.json"

    def get(self, job_id: str) -> DecisionSnapshot | None:
        from botocore.exceptions import ClientError

        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=self.key(job_id)
            )
        except ClientError as error:
            if is_missing_key(error, bucket=self.bucket, key=self.key(job_id)):
                return None
            raise
        try:
            return DecisionSnapshot.model_validate_json(response["Body"].read())
        except ValidationError:
            # Preserve the malformed object through S3 versioning, but allow
            # the next decision to repair the active document using the
            # current contract instead of blocking the operator forever.
            return None

    def save(self, snapshot: DecisionSnapshot) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self.key(snapshot.job_id),
            Body=snapshot.model_dump_json().encode("utf-8"),
            ContentType="application/json",
        )
