"""Shared interpretation of S3 ClientError codes.

S3 deliberately masks "key does not exist" as 403 AccessDenied when the caller
lacks ``s3:ListBucket`` on the bucket, so that a principal with object-level
read cannot probe for key existence. That is correct behaviour, and it is also
a trap: a store that only catches 404/NoSuchKey works perfectly against every
key it has already written and fails on the *first* read of a new key.

That is exactly the bug this module exists to prevent. It surfaced as
"Final decision state could not be loaded." on the first decision recorded for
any job, because the Lambda role granted s3:GetObject on ``bucket/*`` but no
s3:ListBucket on the bucket itself.

The fix is the IAM grant in template.yaml. This module makes the failure
diagnosable rather than a generic 503 if the grant is ever dropped again.

Reference: https://docs.aws.amazon.com/AmazonS3/latest/userguide/troubleshoot-403-errors.html
"""

from typing import Any

MISSING_KEY_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
ACCESS_DENIED_CODES = frozenset({"403", "AccessDenied", "Forbidden"})


class S3PermissionError(RuntimeError):
    """Raised when S3 denies a read that the role is expected to be allowed."""


def error_code(error: Any) -> str:
    return str(error.response.get("Error", {}).get("Code", "")) if (
        getattr(error, "response", None)
    ) else ""


def is_missing_key(error: Any, *, bucket: str, key: str) -> bool:
    """Return True when the error means "this key is not there".

    Raises ``S3PermissionError`` for AccessDenied rather than reporting the
    object as absent: treating a permission failure as "not found" would let a
    misconfigured role silently look like an empty store, which for a decision
    or idempotency record is a correctness bug, not a cosmetic one.
    """

    code = error_code(error)
    if code in MISSING_KEY_CODES:
        return True
    if code in ACCESS_DENIED_CODES:
        raise S3PermissionError(
            f"S3 denied reading s3://{bucket}/{key}. If the object simply does "
            f"not exist, this is the classic missing-s3:ListBucket case: S3 "
            f"returns AccessDenied instead of NoSuchKey unless the role holds "
            f"s3:ListBucket on the bucket itself, not just s3:GetObject on "
            f"bucket/*. Check the function policy in template.yaml."
        ) from error
    return False
