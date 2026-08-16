# Verification Record

This report separates three things that are easy to conflate: what automation
verifies on every commit, what was verified by hand against a live AWS
deployment, and what still needs re-verification. Anything not listed here is
not claimed.

## 1. Verified automatically on every commit

CI runs all of the following before `sam deploy` is reached. A failure in any of
them blocks the deployment.

| Check | Command | Covers |
| --- | --- | --- |
| Unit and integration tests | `pytest` | Scoring, confidence policy, decisions, adapters, worker branches, redaction, logging, the AWS override authorization path |
| Static validation | `python scripts/validate_project.py` | Python parse and import resolution, HTML↔JS element binding for both dashboards, form submit wiring, Lambda packaging boundary, documentation claims vs template, fixture integrity, CI ordering |
| Scoring invariants | `python scripts/simulate_scoring.py` | Runs the real scorer over every fixture scenario; asserts score and confidence bounds, contiguous ranks, ordering, and that an auto-ready result carries no review reasons |
| Browser model | `node scripts/check_browser_model.mjs` | Executes `frontend/local-ai.js` for real: convergence, ground-truth recovery, prediction bounds, and that annotation never reorders the AWS ranking |
| Fixture reproducibility | `git diff --exit-code data/training.json` after regeneration | The committed training fixture matches its generator |
| Template validity | `sam validate --lint` | CloudFormation and SAM correctness |

Python is tested on 3.11 and 3.12 in the pull-request workflow. The SAM runtime
is 3.12.

## 2. Verified by hand against the live AWS stack

These were confirmed against the deployed stack **before the current revision**.
They exercised the same architecture but an earlier build of the code.

- Stack `testproject-vendor-dispatch` in `us-east-1` reached
  `UPDATE_COMPLETE`; all expected resources present and no orphaned functions.
- Anonymous API calls rejected; signed calls succeeded.
- `JobCreated` → SQS → Lambda → `VendorRecommendationGenerated` round trip
  completed; input queue and DLQ both empty afterwards.
- Recommendation and override audit records written to S3 with AES-256
  server-side encryption; public access blocked; bucket policy denies non-TLS.
- SQS server-side encryption enabled; Lambda log retention 14 days.
- API Lambda holds no SQS permission on the recommendation queue.
- Cognito user pool, app client, and domain live; GitHub Pages published and
  loading.
- GitHub Actions OIDC role assumption working with no AWS keys in GitHub.

## 3. Requires re-verification after the next deploy

The current revision changes the deployment shape, so the checks above do not
carry over unmodified. Re-run these after the next `main` push:

- [ ] **`CodeUri` is now `src/`.** Confirm both functions still import cleanly
      and that the package no longer contains `docs/`, `tests/`, `frontend/`, or
      an audit log. `scripts/validate_project.py` checks the source tree; only a
      real build confirms the artifact.
- [ ] **`GET /health` is now unauthenticated.** Confirm it returns 200 without a
      token and that every other route still returns 401 without one.
- [ ] **Idempotency uses S3 conditional writes.** Confirm the deployed botocore
      supports `IfNoneMatch` — if it does not, the store logs a degraded mode
      rather than failing. Send the same `JobCreated` twice and confirm one
      recommendation.
- [ ] **Structured logs.** Confirm CloudWatch shows one JSON object per decision
      and that no customer field appears in any log line.
- [ ] **Override attribution.** Confirm the recorded `actor_id` is the Cognito
      subject and not the value sent in the request body.
- [ ] **Failure traces.** Force a worker failure and confirm the run trace moves
      to `failed` rather than hanging at `running`.
- [ ] Interactive Cognito sign-up and email verification from the public Pages
      URL. This is the only step no automation covers.

Use `python scripts/api_smoke_test.py` for the first, second, and fifth items;
it checks them directly and reports pass/fail per assertion.

## 4. Not implemented

Listed here so their absence is not mistaken for an untested feature. The full
list with reasoning is in [`STATUS.md`](STATUS.md).

- Load, concurrency, and abuse testing.
- Production ML training, holdout validation, and drift monitoring.
- Transactional compare-and-set for decision state (needs DynamoDB).


## 5. Cost posture

Resources remain deployed for review. Every service in the stack is
request-priced; no managed ML endpoint, DynamoDB table, NAT gateway, or VPC is
provisioned. See [`docs/operations.md`](docs/operations.md) for the reasoning
and [`AWS_RESOURCE_TRACKER.md`](AWS_RESOURCE_TRACKER.md) for teardown, including
the deliberately retained audit bucket.

## Conclusion

The architecture is verified end to end and the test suite now gates
deployment, which it did not before. The current revision has been verified
statically and by executing the scoring and browser-model code paths directly;
the AWS-side checklist in section 3 is outstanding and should be completed on
the next deploy before the submission is considered fully re-verified.
