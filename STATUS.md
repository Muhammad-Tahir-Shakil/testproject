# Status

## Implemented

**Contracts and scoring**

- [x] Typed `Job`, `VendorProfile`, `ScoreFactors`, `Recommendation` contracts
      with `extra="forbid"`, so an unexpected field is rejected rather than
      silently stored.
- [x] Ranked top 3–5 eligible vendors with a full factor breakdown.
- [x] Deterministic human-readable rationale per candidate.
- [x] Evidence-based confidence: requirement fit gates the result, evidence
      quality (sample size and staleness) modulates it, and neither reads the
      score. A vendor can rank first and still abstain.
- [x] Decision margin between rank 1 and rank 2 on every response.
- [x] Three independent review triggers with specific reasons: policy
      (high-risk), low confidence, and close call.
- [x] Status derived from `review_reasons` rather than from the score, so a new
      policy cannot be forgotten in the status calculation.
- [x] Bounded model blend behind the eligibility gate; the model cannot promote
      an ineligible vendor and cannot raise confidence.

**Events and workflow**

- [x] `JobCreated` input and `VendorRecommendationGenerated` output events.
- [x] Idempotent SQS consumption via S3 conditional writes (`IfNoneMatch`), so a
      redelivered message produces no duplicate recommendation or audit record.
- [x] Partial batch failure reporting with a DLQ.
- [x] Correlated browser → Cognito → API Gateway → Lambda → SQS/S3 → browser
      run trace, including failure states.
- [x] Versioned final-decision state: AI confirmation vs human override,
      idempotent repeat submissions, and a revision history per job.

**Machine learning**

- [x] Closed-form ridge fit in the standard library; scikit-learn removed.
- [x] Versioned JSON model artifact with embedded training provenance. No
      pickle.
- [x] Identical pure-Python predictor in the dashboard and in Lambda, gated by
      `SCORING_MODEL_ARTIFACT`.
- [x] Reproducible synthetic training fixture from a seeded generator; CI fails
      on any diff.
- [x] Graceful degradation to rules when an artifact is missing or invalid.

**Security and governance**

- [x] Cognito JWT authorization on every route except `GET /health`.
- [x] Override actor taken from the verified Cognito `sub`, never the request
      body; local attribution comes from a header, not an editable field.
- [x] Destructive local routes require explicit confirmation and an optional
      shared token.
- [x] Two-tier PII boundary: the operational store keeps what an operator needs,
      the durable audit trail redacts identifiers and free text while retaining
      every piece of decision evidence.
- [x] Structured JSON logs carrying `request_id`, `model_version`,
      `decision_state`, confidence and margin, with customer fields excluded.
- [x] Lambda package scoped to `src/`; design documents, tests, the dashboard,
      and local audit logs no longer ship to AWS.
- [x] Encrypted SQS, TLS-only versioned S3, public access blocked, GitHub OIDC
      deployment with no long-lived keys.

**Verification**

- [x] CI runs `pytest`, the static validator, the scoring simulation, and the
      browser-model check before anything reaches AWS.
- [x] Separate pull-request workflow on Python 3.11 and 3.12.
- [x] Tests cover the AWS override authorization path, the worker trace and
      idempotency branches, redaction including the list path, model artifact
      failure modes, and logging PII exclusion.
- [x] Static validator cross-checks every `getElementById` target against the
      HTML for both dashboards, plus the packaging boundary and documentation
      claims against the template.

## Deliberate scope decisions

Recorded with reasoning in [`docs/platform-decisions.md`](docs/platform-decisions.md):

- The runnable slice is AWS; the Azure design is the Part 1 deliverable.
- No managed ML endpoint is provisioned; the model runs locally.
- No DynamoDB: idempotency and decision state use S3 conditional writes and
  versioning.
- No EventBridge, Step Functions, VPC, NAT Gateway, or CloudFront.

## Known gaps

These are real, and are not claimed as done anywhere else in the repository.

**Concurrency**

- [ ] The S3 decision store is not a transactional compare-and-set under
      concurrent API Lambda invocations. Bucket versioning preserves history,
      but a strict single-active-decision guarantee needs DynamoDB with a
      conditional version write.
- [ ] The run trace is a read-modify-write on one S3 key. Writes are batched to
      narrow the window, but a concurrent worker update and override can still
      interleave.

**Model lifecycle**

- [ ] Trained on synthetic fixture data; R² is in-sample. No holdout,
      cross-validation, calibration curve, or fairness slice.
- [ ] No drift detection, shadow deployment, canary, or automated retraining.
      Structured logs make the first pass queryable; alerting is not built.
- [ ] Overrides are captured with everything a retraining job would need, but
      nothing consumes them yet.
- [ ] No exploration policy, so a highly-ranked vendor accumulates evidence
      while a new vendor never does.

**Production hardening**

- [ ] No load, concurrency, or abuse testing.
- [ ] IAM policies are least-privilege for the demo but have had no formal
      production review.

**Data model**

- [ ] The vendor pool is supplied by the caller rather than read from a system
      of record, so the service holds no authoritative vendor data and cannot
      detect a stale caller snapshot.
- [ ] No retention, deletion, or data-subject-request tooling. Do not send real
      customer data.