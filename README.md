# RetailFixIt — Vendor Scorecard & Intelligent Dispatch

An explainable vendor recommendation service for an event-driven dispatch
platform: ranked candidates, a factor-level breakdown, a human-readable
rationale, and an explicit policy layer that decides when a human — not the
system — has to make the call.

## Read this first

Two decisions depart from a literal reading of the brief. Both are deliberate
and both are set out in **[`docs/platform-decisions.md`](docs/platform-decisions.md)**:

1. **The Part 1 design is Azure-native as requested; the runnable Part 2 slice
   is AWS.** No Azure subscription was available inside the assessment window
   and an already-credited AWS account was. The domain layer sits behind
   `EventBus`, `AuditSink`, `TraceStore`, and `DecisionStore` protocols, and no
   module that scores, ranks, or explains a recommendation imports boto3 — so
   the port is five adapter classes plus a host, not a rewrite. The full service
   mapping is in that document.
2. **The model is trained and served locally, not on a managed ML endpoint.** A
   managed endpoint bills for provisioned time rather than for predictions,
   which for a review stack is the largest line item and the one most likely to
   keep billing after the review ends. The artifact is versioned JSON, inference
   is dependency-free pure Python, and the identical predictor runs in the
   dashboard and in Lambda — enabling it is the `SCORING_MODEL_ARTIFACT`
   environment variable, not a rewrite.

## What it does

A `JobCreated` event carries a job and a vendor snapshot. The service filters
ineligible vendors, scores the rest on nine visible weighted factors, blends in
a bounded model correction where one is configured, and returns the top 3–5 with
a factor breakdown and a rationale. It then answers a second, separate question:
**is this decision safe to act on automatically?**

Score and confidence deliberately take different inputs. Confidence never reads
the score — it comes from requirement fit (the *weakest* satisfied hard
requirement) and evidence quality (how much history the vendor's performance
rates rest on, and how stale that history is). A vendor can rank first and still
be low-confidence, which is exactly the case that matters.

Three conditions route a job to a human, each reported as a specific reason:

- **Policy** — high-risk jobs never auto-dispatch, whatever the score.
- **Low confidence** — thin or stale evidence, or an unmet hard requirement.
- **Close call** — rank 1 does not clearly beat rank 2, so the ranking is a tie
  broken on noise.

An empty `review_reasons` list is the only condition under which the system
describes itself as ready to auto-dispatch.

See all of it without opening a browser:

```bash
python scripts/simulate_scoring.py
```

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,aws]'
pytest
python scripts/validate_project.py
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/`, click **Setup local environment**, then run the
sample and manual requests and record a decision. Full guide:
[`docs/setup.md`](docs/setup.md).

## Public hybrid dashboard

<https://muhammad-tahir-shakil.github.io/testproject/>

Cognito sign-in → browser-local model training → authenticated API Gateway call
→ Lambda scoring → SQS and S3 → a live step-by-step trace rendered in the
browser. The browser model appears as a *second opinion* beside the AWS result
and never reorders it; when the two disagree the dashboard says so, because the
AWS ranking is the one written to the audit trail.

## Architecture and reasoning

- **Part 1 — Azure design:** [`docs/architecture.md`](docs/architecture.md)
- **Part 2 — AWS implementation:** [`docs/aws-architecture.md`](docs/aws-architecture.md)
- **Part 3 — governance answers:** [`answers.md`](answers.md)
- Platform decisions: [`docs/platform-decisions.md`](docs/platform-decisions.md)
- Security and the PII boundary: [`docs/security.md`](docs/security.md)
- Criteria map: [`docs/assessment-map.md`](docs/assessment-map.md)
- Operations and cleanup: [`docs/operations.md`](docs/operations.md)
- Resource inventory: [`AWS_RESOURCE_TRACKER.md`](AWS_RESOURCE_TRACKER.md)
- Current status and known gaps: [`STATUS.md`](STATUS.md)

## Repository layout

```
src/app/           Domain: contracts, scoring, decisions, service, adapters
src/app/static/    Local admin dashboard (served by FastAPI)
src/lambda_*.py    AWS handler entry points
frontend/          GitHub Pages hybrid dashboard
template.yaml      SAM stack (CodeUri scoped to src/)
infra/             GitHub OIDC deployment role
scripts/           Data generation, scoring simulation, validation, smoke tests
data/              Sample contracts and the synthetic training fixture
tests/             Automated tests
```

`CodeUri` is scoped to `src/` deliberately: SAM CLI has no `.samignore`, so
`CodeUri: .` previously packaged the design documents, the test suite, and a
local audit log into both Lambda functions.

## Verification

- `pytest` — unit and integration tests, including the AWS override
  authorization path and the redaction boundary.
- `python scripts/validate_project.py` — dependency-free checks pytest cannot
  express: HTML/JS element-ID binding for both dashboards, the Lambda packaging
  boundary, documentation claims against the template, and fixture integrity.
- `python scripts/simulate_scoring.py` — runs the real scorer over every
  scenario and asserts the decision invariants.
- `python scripts/api_smoke_test.py` — exercises the deployed API with a real
  Cognito token.

CI runs the first three before anything reaches AWS.

## Cost boundary

No managed ML endpoint, DynamoDB, EventBridge, Step Functions, VPC, NAT
Gateway, or CloudFront is provisioned. Idempotency uses S3 conditional writes
instead of a DynamoDB table for the same reason. Cleanup instructions:
[`docs/operations.md`](docs/operations.md).
