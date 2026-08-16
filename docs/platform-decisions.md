# Platform Decisions

Two decisions depart from the most literal reading of the brief. Both were
deliberate, and both are recorded here rather than buried in a footnote.

---

## 1. The runnable implementation is AWS, not Azure

**What was delivered.** Part 1 asked for an Azure-native design — diagram,
services and why, tradeoffs. That is delivered as specified in
[`architecture.md`](architecture.md): Event Grid and Service Bus for ingestion,
Durable Functions for orchestration, Azure ML for the model lifecycle, Blob
Storage for the audit trail, managed identity between services.

Part 2 asked for a working slice in Azure-aligned technologies. That slice runs
on AWS. This is a deviation, stated plainly so a reviewer can weigh it rather
than discover it.

**Why.** Azure account was blocked during payment setup update and no other Azure subscription was available inside the assessment window; Billing verification for updated payment method required support contact and time so i switched to AWS.
An Azure Function triggered by a Service Bus message and a Lambda triggered by an SQS message are the same design
decision with two spellings. The time saved went into the confidence and abstention model, the human-approval policy, idempotent event handling, the redaction boundary, and a test suite CI actually runs.

**Why this is a trade rather than a shortcut.** "It would port cleanly" is easy
to assert and usually false. Here it is checkable:

- No module that computes, explains, or ranks a recommendation imports boto3.
  `scoring.py`, `service.py`, `models.py` and `decisions.py` are pure domain
  code. Cloud access sits behind four protocols: `EventBus` and `AuditSink`
  (`events.py`), `TraceStore` (`run_trace.py`), `DecisionStore` (`decisions.py`).
- The AWS-specific code is five small adapters: `AwsSqsEventBus`,
  `AwsS3AuditLogger`, `AwsRunTraceStore`, `S3DecisionStore`, `IdempotencyStore`.
- The tests prove the seam. Every adapter test injects a fake client and the
  domain tests touch no cloud SDK — they could not run offline otherwise.

A port is five adapter classes against the Azure SDKs, a `function_app.py` host,
and a Bicep template. The FastAPI application, every contract, the scorer, the
explainability layer, the governance policy and the whole test suite move
unchanged.

| Design role | Azure (design) | AWS (implemented) |
| --- | --- | --- |
| Event bus | Service Bus, Event Grid | SQS with DLQ |
| Compute | Azure Functions | Lambda |
| Orchestration | Durable Functions | SQS + Lambda (single-step slice) |
| HTTP edge | API Management / Functions HTTP | API Gateway HTTP API |
| Identity | Entra ID / AD B2C | Cognito + JWT authorizer |
| Audit store | Blob Storage (immutable, versioned) | S3 (versioned, SSE, TLS-only) |
| Idempotency / decision state | Table Storage or Cosmos DB | S3 conditional writes (`IfNoneMatch`) |
| Model lifecycle | Azure ML registry and endpoints | Not deployed — see below |
| Logs and metrics | Azure Monitor / App Insights | CloudWatch structured JSON |
| Deployment identity | Workload identity federation | GitHub OIDC |
| IaC | Bicep | SAM / CloudFormation |

**What a reviewer should take from this.** If Azure specifically was the point,
this submission does not satisfy it and `architecture.md` is the only Azure
artifact. If the point was event-driven AI system design with explainability and
human oversight, the implementation demonstrates it, and the cloud is arranged
as a swappable detail rather than a rewrite.

---

## 2. The model is trained and served locally, not on a managed ML service

**The decision.** No SageMaker or Azure ML endpoint is provisioned. The deployed
stack runs the transparent rules baseline (`rules-v1`); the trained model runs in
the local dashboard and, as a second opinion, in the browser.

**Why.** A managed real-time endpoint is billed for provisioned time, not for
predictions — it costs the same idling overnight as serving traffic. For a
review stack handling a few dozen requests that is the largest line item and the
one most likely to keep billing after the review ends. Every other service here
is request-priced and costs approximately nothing at rest. That asymmetry is the
whole reason, and it is the same reasoning that keeps DynamoDB, Step Functions,
EventBridge, NAT Gateway and CloudFront out of the template. Separately, a
nine-feature linear model fitted to a synthetic fixture does not warrant a
hosted endpoint; serving it behind one would demonstrate the ability to spend
money, not to design a system.

**What was built instead.** The constraint became a design property:

- The fit is closed-form ridge in the standard library (`fit_ridge`, ~40 lines),
  which removed scikit-learn — a ~90 MB dependency — from the project entirely.
- The artifact is plain JSON: schema, feature names, coefficients, intercept,
  alpha, row count, provenance. Not a pickle. Unpickling a model file is
  arbitrary code execution, and a linear model has nothing that needs pickling.
  JSON is also reviewable in a pull request and diffable between versions —
  the cheapest possible model registry.
- Inference is dependency-free pure Python, so the identical predictor runs in
  the dashboard and inside Lambda. Deployment is a toggle
  (`SCORING_MODEL_ARTIFACT`), not a rewrite. A missing or invalid artifact logs
  `model.artifact_rejected` and degrades to rules rather than failing.
- The blend is bounded: `model_weight` defaults to 0.2 and is clamped, and the
  eligibility gate runs first, so the model cannot promote an inactive or
  at-capacity vendor whatever it predicts.
- The browser model never changes the ranking. It renders as a second opinion
  beside the AWS score, with a banner when the two disagree. Re-sorting cards by
  a score that exists only in one operator's browser session would mean the UI
  showed a different order from the audit trail.

**Reproducibility.** `data/training.json` comes from
`scripts/generate_training_data.py` — a declared ground-truth weight vector plus
seeded noise. CI regenerates it and fails on any diff. The generator holds
`availability` at 1.0 deliberately: the eligibility gate filters unavailable
vendors before the model is consulted, so training on them would be train/serve
skew. The column is then constant, ridge shrinks its coefficient to zero and the
intercept absorbs the level — asserted in `tests/test_local_ml.py`.

**Honest limits.** Labels come from a declared linear function plus noise, so
the fit can only rediscover that function; recovering it evidences a working
pipeline, not predictive power on real vendors. The reported R² is in-sample —
no holdout, cross-validation, calibration curve or fairness slice. No drift
detection, shadow deployment, canary or retraining pipeline. Overrides capture
everything a retraining job would need, but nothing consumes them yet.

**With a budget:** provision an endpoint, register the artifact with lineage back
to the training run, shadow against `rules-v1` for a fixed window, compare
acceptance and override rate rather than offline loss, then canary with rollback
on business-metric regression. The blend seam and the `model_version` field on
every recommendation and audit record are what make that possible without a
contract change.
