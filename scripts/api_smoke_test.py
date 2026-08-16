"""Exercise the deployed API the way a real client does: with a Cognito token.

Replaces the earlier SigV4 smoke test, which signed for ``execute-api``. That
was right for the original IAM authorizer but wrong since the stack moved to a
Cognito JWT authorizer -- every call it made returned 401, including the ones
the setup guide told you to run.

Usage:

    python scripts/api_smoke_test.py \
      --api-url "$API_URL" \
      --user-pool-id "$USER_POOL_ID" \
      --client-id "$CLIENT_ID" \
      --username you@example.com \
      --sample-file data/sample.json \
      --test-override

The password is read from the ``COGNITO_PASSWORD`` environment variable, never
from a command-line argument, so it does not land in shell history or process
listings.

Checks performed:

1. Anonymous ``GET /health`` succeeds (the one deliberately open route).
2. Anonymous ``POST /recommendations`` is rejected.
3. Authenticated ``POST /recommendations`` returns a ranked, explained result.
4. Authenticated ``POST /overrides`` records a decision (with --test-override).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class SmokeTestFailure(RuntimeError):
    pass


def http_json(
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: int = 30,
) -> tuple[int, Any]:
    """Return (status, body). Never raises on a non-2xx.

    ``urlopen`` raises HTTPError for any non-2xx, which the previous script did
    not handle -- so a 401 produced a traceback instead of the response body
    that would have explained it.
    """

    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"content-type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8")
        try:
            return error.code, json.loads(raw or "null")
        except json.JSONDecodeError:
            return error.code, raw
    except urllib.error.URLError as error:
        raise SmokeTestFailure(f"Could not reach {url}: {error.reason}") from error


def cognito_id_token(
    user_pool_id: str, client_id: str, username: str, password: str, region: str
) -> str:
    """Exchange username/password for an ID token via ADMIN_USER_PASSWORD_AUTH."""

    import boto3

    client = boto3.client("cognito-idp", region_name=region)
    response = client.admin_initiate_auth(
        UserPoolId=user_pool_id,
        ClientId=client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    if "AuthenticationResult" not in response:
        raise SmokeTestFailure(
            f"Cognito returned a challenge rather than a token: "
            f"{response.get('ChallengeName')}. Complete it in the hosted UI first."
        )
    # The API Gateway authorizer validates the ID token audience against the
    # app client, so the ID token is the correct one to send -- not the access
    # token, whose audience is the resource server.
    return response["AuthenticationResult"]["IdToken"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--user-pool-id")
    parser.add_argument("--client-id")
    parser.add_argument("--username")
    parser.add_argument("--region", default=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
    parser.add_argument("--sample-file", default="data/sample.json")
    parser.add_argument("--test-override", action="store_true")
    parser.add_argument(
        "--token",
        default=os.getenv("COGNITO_ID_TOKEN"),
        help="Use an existing ID token instead of signing in.",
    )
    args = parser.parse_args()

    base_url = args.api_url.rstrip("/")
    failures: list[str] = []

    def report(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{f' -- {detail}' if detail else ''}")
        if not ok:
            failures.append(name)

    print(f"\nSmoke testing {base_url}\n")

    print("Unauthenticated surface")
    status, body = http_json(f"{base_url}/health")
    report(
        "GET /health is reachable without a token",
        status == 200 and isinstance(body, dict) and body.get("status") == "ok",
        f"status {status}",
    )
    if isinstance(body, dict):
        print(
            f"         model={body.get('model_version')} "
            f"mode={body.get('scoring_mode')} "
            f"confidence>={body.get('confidence_threshold')} "
            f"margin>={body.get('margin_threshold')}"
        )

    status, _ = http_json(f"{base_url}/recommendations", "POST", {"job": {}, "vendors": []})
    report(
        "POST /recommendations rejects anonymous callers",
        status in (401, 403),
        f"status {status}",
    )

    token = args.token
    if not token:
        password = os.getenv("COGNITO_PASSWORD")
        if not (args.user_pool_id and args.client_id and args.username and password):
            print(
                "\nSkipping authenticated checks: supply --user-pool-id, --client-id,\n"
                "--username and the COGNITO_PASSWORD environment variable, or set\n"
                "COGNITO_ID_TOKEN to an existing token."
            )
            return 1 if failures else 0
        try:
            token = cognito_id_token(
                args.user_pool_id, args.client_id, args.username, password, args.region
            )
        except SmokeTestFailure as error:
            report("Cognito sign-in", False, str(error))
            return 1

    print("\nAuthenticated surface")
    status, body = http_json(f"{base_url}/health", token=token)
    report("GET /health with a token", status == 200, f"status {status}")

    sample_path = Path(args.sample_file)
    if not sample_path.exists():
        report("Sample payload exists", False, str(sample_path))
        return 1
    payload = json.loads(sample_path.read_text(encoding="utf-8"))

    status, body = http_json(f"{base_url}/recommendations", "POST", payload, token=token)
    ok = status == 200 and isinstance(body, dict) and bool(body.get("recommendations"))
    report("POST /recommendations returns a ranked result", ok, f"status {status}")
    if ok:
        top = body["recommendations"][0]
        print(
            f"         #1 {top['vendor_name']} score={top['score']} "
            f"confidence={top['confidence']} status={body['status']}"
        )
        print(f"         rationale: {top['rationale']}")
        for reason in body.get("review_reasons", []):
            print(f"         review: {reason}")
        report(
            "Response carries an explanation for every candidate",
            all(item.get("rationale") for item in body["recommendations"]),
        )
        report(
            "Response reports its model version",
            bool(body.get("model_version")),
            body.get("model_version", ""),
        )

    if args.test_override and ok:
        status, override = http_json(
            f"{base_url}/overrides",
            "POST",
            {
                "job_id": payload["job"]["job_id"],
                "vendor_id": payload["vendors"][1]["vendor_id"],
                "reason": "Smoke-test manual override",
                # Replaced server-side with the verified Cognito subject.
                "actor_id": "smoke-test",
            },
            token=token,
        )
        report("POST /overrides records a decision", status == 200, f"status {status}")
        if status == 200 and isinstance(override, dict):
            report(
                "Override actor is the Cognito subject, not the request body",
                override.get("actor_id") not in (None, "", "smoke-test"),
                f"actor_id={override.get('actor_id')}",
            )

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("PASSED: all smoke checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
