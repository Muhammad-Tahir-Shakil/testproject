# Security, PII, and Credentials

## Browser security

- GitHub Pages contains public identifiers only.
- No AWS access key is sent to the browser.
- Cognito issues the user JWT.
- API Gateway validates the Cognito JWT.
- Browser local ML stores model weights in `sessionStorage`.
- Customer PII is not stored in browser storage.

## AWS controls

- S3 Public Access Block is enabled.
- S3 denies non-TLS requests.
- S3 audit objects use server-side encryption.
- SQS queues use server-side encryption.
- SQS input has a DLQ.
- Lambda uses execution roles; the API Lambda has no permission on the
  recommendation queue.
- Every API route requires a Cognito JWT except `GET /health`, which returns
  only the model version and thresholds and exists for uptime monitoring.
- CORS is restricted to the Pages origin.
- The SQS worker claims each event ID with an S3 conditional write, so a
  redelivered message cannot produce a duplicate recommendation or audit record.
- Lambda logs have bounded retention.
- IAM deployment uses GitHub OIDC.
- GitHub Actions uses a restricted CloudFormation service role.

## PII boundary

There are two stores with deliberately different contents. Conflating them is
the usual way an audit trail becomes a data-subject-request problem.

**The operational store** (the `jobs` and `vendors` tables locally; the job
record in a real deployment) holds the request as the dispatcher entered it,
including `customer_name`, `site_name`, `asset_label`, `title`, and `details`.
An operator cannot triage a job they cannot read, so this data exists and is
protected by access control rather than by removal.

**The audit trail** (S3 in AWS, the `audit` table locally) is durable,
long-retention, and redacted before anything is written:

| Field group | Treatment | Examples |
| --- | --- | --- |
| Customer and site identifiers | Removed | `customer_name`, `customer_email`, `customer_phone`, `address`, `phone`, `email`, `site_name`, `actor_email` |
| Operator free text | Removed | `title`, `details`, `asset_label` |
| Decision evidence | Retained | `job_type`, `required_skills`, every `ScoreFactors` value, `score`, `confidence`, `requirement_fit`, `evidence_strength`, `rationale`, `model_version`, `review_reasons` |

The decision remains fully explainable without the removed fields, because
everything the scorer actually consumed sits in the retained group. Free text is
removed rather than kept because a dispatcher can and does paste a contact name
or a gate code into a `details` box.

`redact()` accepts a `retain_free_text` flag for local debugging. It is never
enabled on the AWS path — `AwsS3AuditLogger` hard-codes `False`.

### What the scorer can see

The scorer never reads customer identity. `Job.customer_name` is accepted by the
contract because the dispatcher UI displays it, but it is not a factor and never
appears in `ScoreFactors`.

`title` and `details` do influence ranking, through a fixed vocabulary of
service terms in `scoring.TEXT_SKILL_ALIASES`. Only vocabulary matches are
extracted; the raw strings never reach `ScoreFactors` or the audit record. That
keeps free-text PII out of the feature path while still letting a dispatcher's
description change the result.

`model_config = ConfigDict(extra="forbid")` on `Job`, `VendorProfile`,
`RecommendationRequest`, and `OverrideRequest` means an unexpected field is
rejected with a 422 rather than silently stored, so a caller cannot smuggle
`customer_ssn` into the audit trail by adding it to the payload. Asserted in
`tests/test_hybrid_security.py`.

The sample and training fixtures are synthetic.

Do not use real customer data until retention, deletion, DLP, access review,
and incident response controls are approved.

## Actor attribution

An audit record is worthless if the caller can set the actor field.

- **AWS**: `POST /overrides` overwrites `actor_id` with the `sub` claim from the
  API Gateway JWT authorizer context; a body-supplied `actor_id` is discarded,
  and a request without a verified subject is rejected with 401.
- **Local**: there is no identity provider, so the dashboard does the next best
  thing. The actor comes from an `X-Dispatcher-Id` request header rather than an
  editable form field, and the body value is discarded the same way. This is not
  authentication and is not claimed to be; it makes attribution deliberate and
  keeps the local audit shape identical to production.
- **Destructive local routes** (`POST /api/local/reset`) additionally require an
  `X-Confirm-Reset: reset` header, plus an `X-Local-Admin-Token` matching
  `LOCAL_ADMIN_TOKEN` when that variable is set. When it is not set, the service
  logs `local.privileged_route_unprotected` on every use.

## Deployment package boundary

`sam build` packages the directory named by `CodeUri`, and SAM CLI has no
`.samignore`. `CodeUri` is therefore scoped to `src/`, which contains only the
two handler modules, the `app` package, and `requirements.txt`.

Under the previous `CodeUri: .` the artifact included the assessment brief,
design documents, the test suite, the browser dashboard, and a local audit log
holding real recommendation payloads. `scripts/validate_project.py` fails the
build if `CodeUri` regresses to `.`, or if the packaged directory gains `docs/`,
`tests/`, `frontend/`, `runtime/`, or an audit log.

## Credential rules

- Never commit AWS keys.
- Never put AWS keys in GitHub variables or secrets.
- Use AWS OIDC for GitHub Actions.
- Use Cognito for browser users.
- Use local AWS profiles only for administrator setup.
- Rotate any key exposed in chat, terminals, logs, or screenshots.

## Deployment permissions

The GitHub OIDC role:

- Is restricted to this repository and `github-pages` environment.
- Can manage CloudFormation deployment.
- Can pass only the testproject CloudFormation service role.
- Can access the SAM artifact bucket.

The CloudFormation service role:

- Creates the assessment stack resources.
- Reads Lambda package artifacts.
- Manages Cognito, Lambda, SQS, S3, API Gateway, logs, and application IAM
  roles required by the template.

Review:

- `template.yaml`
- `infra/github-oidc-role.yaml`
- `AWS_RESOURCE_TRACKER.md`

## Known production follow-ups

- WAF and rate limiting.
- Secrets Manager/KMS-managed application secrets.
- Private networking if required.
- Load and abuse testing.
- Production ML validation and drift monitoring.
- Formal IAM review.
