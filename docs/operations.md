# Operations and Navigation

## Run locally

- Setup: [`docs/setup.md`](setup.md)
- Local dashboard: `http://127.0.0.1:8000/`
- Local state: gitignored `runtime/` (`dispatch.db`, `local_model.json`,
  `audit.jsonl`); override the location with `RUNTIME_DIR`
- Tests: `pytest`
- Static validation: `python scripts/validate_project.py`
- Scoring behaviour as a table: `python scripts/simulate_scoring.py`
- Browser model executed outside a browser: `node scripts/check_browser_model.mjs`

## Run AWS

- Deployment: [`docs/setup.md`](setup.md)
- Infrastructure: [`template.yaml`](../template.yaml)
- Deployment role: [`infra/github-oidc-role.yaml`](../infra/github-oidc-role.yaml)
- GitHub workflow: [`../.github/workflows/deploy.yml`](../.github/workflows/deploy.yml)

## Understand the system

- **Platform decisions (start here):** [`docs/platform-decisions.md`](platform-decisions.md)
- Azure assessment design: [`docs/architecture.md`](architecture.md)
- AWS implementation: [`docs/aws-architecture.md`](aws-architecture.md)
- Criteria mapping: [`docs/assessment-map.md`](assessment-map.md)
- Governance answers: [`../answers.md`](../answers.md)

## Security and cleanup

- Security and PII: [`docs/security.md`](security.md)
- Resource inventory: [`../AWS_RESOURCE_TRACKER.md`](../AWS_RESOURCE_TRACKER.md)
- Final verification: [`../FINAL_REPORT.md`](../FINAL_REPORT.md)
- Current status: [`../STATUS.md`](../STATUS.md)

## Cost control

Every service in the stack is request-priced and costs approximately nothing at
rest. The omissions below are what keep it that way; the reasoning is in
[`docs/platform-decisions.md`](platform-decisions.md).

- **No managed ML endpoint.** A real-time inference endpoint bills for
  provisioned time rather than for predictions, which would make it the largest
  line item and the one most likely to keep billing after the review. The model
  is trained and served locally instead.
- **No DynamoDB.** Idempotency uses S3 `PutObject` with `IfNoneMatch`, which is
  an atomic compare-and-set; decision state uses versioned S3 documents. Add
  DynamoDB conditional writes before concurrent production use — see the
  concurrency gaps in [`../STATUS.md`](../STATUS.md).
- **No EventBridge or Step Functions.** The slice is a single-step workflow;
  SQS plus Lambda covers it.
- **No VPC, NAT gateway, or CloudFront.**
- Lambda log retention is bounded at 14 days.
- Delete the assessment stack after review; the audit bucket is retained
  deliberately, so remove it separately.
