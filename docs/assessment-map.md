# Assessment Coverage Map

Read [`platform-decisions.md`](platform-decisions.md) first: it explains why the
runnable slice is AWS rather than Azure, and why the model is trained locally
rather than on a managed endpoint.

## Part 1: AI System Design and Architecture (Azure, conceptual)

| Requirement | Where |
| --- | --- |
| High-level Azure architecture diagram | [`architecture.md`](architecture.md) |
| Azure services used and why | [`architecture.md`](architecture.md) |
| Canonical data model (JobEvent, VendorProfile, ScoreFactors) | [`architecture.md`](architecture.md); implemented in `src/app/models.py` |
| Event ingestion and feature pipelines | [`architecture.md`](architecture.md) |
| Model strategy (rules → ML hybrid) | [`architecture.md`](architecture.md), `src/app/scoring.py` |
| Explainability and rationale generation | `src/app/scoring.py` (`_rationale`) |
| Human-in-the-loop controls | `src/app/scoring.py` (`review_reasons`), `src/app/decisions.py` |
| Model lifecycle (training, validation, drift) | [`architecture.md`](architecture.md), [`platform-decisions.md`](platform-decisions.md) |
| Security, RBAC, PII handling | [`security.md`](security.md) |
| Cost, scalability, failure modes | [`architecture.md`](architecture.md), [`operations.md`](operations.md) |
| Where decisions are automated vs advisory | `review_reasons`; an empty list is the only auto-dispatch condition |
| Key tradeoffs and assumptions | [`platform-decisions.md`](platform-decisions.md); gaps in [`../STATUS.md`](../STATUS.md) |

## Part 2: Hands-On AI + Integration (AWS, runnable)

| Requirement | Where |
| --- | --- |
| Vendor scoring service: job + vendor input | `src/app/service.py`, `src/app/scoring.py` |
| Ranked vendor list (top 3–5) | `evaluate(..., top_k)`, capped 1–5 by the contract |
| Score breakdown (why this vendor) | `ScoreFactors` on every recommendation, plus `rule_score`, `model_score`, `requirement_fit`, `evidence_strength` |
| Hybrid rules + ML (preferred) | Deterministic weights with a bounded model blend behind the eligibility gate; [`platform-decisions.md`](platform-decisions.md) explains why the blend is off in the deployed stack |
| Explainability layer | `_rationale` produces the brief's example form: "Vendor A ranked #1 due to …" |
| `JobCreated` event | `src/app/models.py`, consumed by `src/lambda_function.py` |
| `VendorRecommendationGenerated` event | Published by the worker to SQS |
| Fit into a broader workflow | [`aws-architecture.md`](aws-architecture.md) flow diagram and the live run trace |
| Admin UI: job details, recommendations, rationale, manual override | Both dashboards: `src/app/static/` (local) and `frontend/` (hosted) |
| API + payload examples | [`setup.md`](setup.md), `scripts/api_smoke_test.py`, `data/sample.json` |
| Model versioning strategy | `model_version` on every recommendation and audit record; versioned JSON artifact with embedded provenance |
| Deployment approach | SAM stack in [`../template.yaml`](../template.yaml); OIDC pipeline in `.github/workflows/deploy.yml` |
| Logging of inputs, outputs, overrides | `RecommendationInput`, `RecommendationOutput`, and decision events in the S3/SQLite audit trail |
| Infrastructure as code | [`../template.yaml`](../template.yaml), [`../infra/github-oidc-role.yaml`](../infra/github-oidc-role.yaml) |
| README with approach, assumptions, limitations | [`../README.md`](../README.md), [`../STATUS.md`](../STATUS.md) |

## Part 3: Engineering Reasoning and Governance

All four questions are answered at length in [`../answers.md`](../answers.md),
with each implemented claim linked to the file that implements it:

| Question | Implemented counterpart |
| --- | --- |
| AI authority and risk | `HUMAN_APPROVAL_RISK_LEVELS`, gated confidence, margin threshold |
| Drift and feedback | Structured decision logs; override records carry the original recommendation, actor, reason, and model version |
| Data quality and events | `sample_size` / `data_age_hours` provenance, `extra="forbid"` contracts, S3 conditional-write idempotency |
| Failure modes | Rules fallback, DLQ, partial batch failures, failure-state run trace, explicit `review_reasons` |

## Optional bonus

| Bonus item | Status |
| --- | --- |
| Offline training script | `scripts/generate_training_data.py` plus `LocalModel.train` (closed-form ridge, no third-party dependency) |
| Confidence scoring / abstention logic | Implemented and load-bearing — `_confidence` and `review_reasons` |
| Fairness / bias mitigation discussion | [`../answers.md`](../answers.md) §1 and §2, including the feedback-loop starvation risk |
| A/B testing plan for AI vs manual dispatch | [`../answers.md`](../answers.md) §2 (shadow → canary → rollback on business metrics). Not implemented. |
| SLA-aware optimization | `sla_fit` factor and SLA-driven review reasons. No optimizer. |

## How to demonstrate in five minutes

1. `python scripts/simulate_scoring.py` — every scenario, its decision, and the
   reason a human is required, without starting anything.
2. `uvicorn app.main:app --reload`, open `http://127.0.0.1:8000/`, click
   **Setup local environment**.
3. **Run sample request** — a medium-risk job with a well-evidenced vendor:
   auto-ready, with the full factor breakdown and rationale.
4. On the hosted dashboard, switch to the **office elevator** scenario (high
   risk, thin vendor history) and dispatch: manual review, with both triggering
   reasons listed.
5. Record a decision in the override panel, then submit the same vendor again —
   the repeat is idempotent and creates no new revision.
6. Inspect the audit panel: customer name and free text are redacted, every
   score factor and rationale is retained.
7. `python scripts/validate_project.py` for the packaging, element-binding, and
   documentation checks.
