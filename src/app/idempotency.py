"""At-most-once processing for the SQS worker.

SQS is at-least-once, and a redelivered ``JobCreated`` would write a second set
of audit records and publish a second ``VendorRecommendationGenerated`` --
which a downstream dispatch service reads as two recommendations for one job.

The claim is an S3 ``PutObject`` with ``IfNoneMatch: "*"``: S3 evaluates it
atomically and fails with ``PreconditionFailed`` if the key exists, giving a
compare-and-set primitive without adding DynamoDB.

If the runtime's botocore predates conditional writes the store degrades to
read-then-write and reports it via ``conditional_writes``. That mode is racy
under simultaneous redelivery, so it is surfaced rather than hidden.
"""

import json
from datetime import datetime, timezone
from typing import Any

from .s3_errors import is_missing_key


class IdempotencyStore:
    """Claim an event ID exactly once, backed by an S3 object."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "processed",
        client: Any | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("AUDIT_BUCKET is required")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.client = client
        self.conditional_writes = True

    def key(self, event_id: str) -> str:
        return f"{self.prefix}/{event_id}.json"

    def claim(self, event_id: str, detail: dict[str, Any] | None = None) -> bool:
        """Return True if this call won the claim, False if already processed."""

        from botocore.exceptions import ClientError

        body = json.dumps(
            {
                "event_id": event_id,
                "claimed_at": datetime.now(timezone.utc).isoformat(),
                **(detail or {}),
            }
        ).encode("utf-8")

        arguments: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": self.key(event_id),
            "Body": body,
            "ContentType": "application/json",
        }
        if not self.conditional_writes:
            return self._claim_without_condition(event_id, body)
        arguments["IfNoneMatch"] = "*"

        try:
            self.client.put_object(**arguments)
            return True
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"PreconditionFailed", "ConditionalRequestConflict"}:
                return False
            raise
        except (TypeError, ValueError) as error:
            # botocore raises ParamValidationError (a ValueError subclass) for
            # an unknown parameter on older versions. Retry unconditionally and
            # record that the guarantee has weakened.
            if not self.conditional_writes:
                raise
            if "IfNoneMatch" not in str(error):
                raise
            self.conditional_writes = False
            return self._claim_without_condition(event_id, body)

    def _claim_without_condition(self, event_id: str, body: bytes) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self.key(event_id))
            return False
        except ClientError as error:
            if not is_missing_key(error, bucket=self.bucket, key=self.key(event_id)):
                raise
        self.client.put_object(
            Bucket=self.bucket,
            Key=self.key(event_id),
            Body=body,
            ContentType="application/json",
        )
        return True


class InMemoryIdempotencyStore:
    """Local and test equivalent with the same contract."""

    def __init__(self) -> None:
        self.claimed: set[str] = set()
        self.conditional_writes = True

    def key(self, event_id: str) -> str:
        return f"processed/{event_id}.json"

    def claim(self, event_id: str, detail: dict[str, Any] | None = None) -> bool:
        if event_id in self.claimed:
            return False
        self.claimed.add(event_id)
        return True
