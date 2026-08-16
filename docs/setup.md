# Setup and Deployment

## 1. Local dashboard

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,aws]'
pytest
python scripts/validate_project.py
uvicorn app.main:app --reload
```

There is no `local` extra any more: the model is a closed-form ridge fit in the
standard library, so scikit-learn is no longer a dependency. See
[`platform-decisions.md`](platform-decisions.md).

Open:

```text
http://127.0.0.1:8000/
```

Click **Setup local environment**, then **Run sample request**. Try **Run manual
request** too — edit the form first and note that the browser enforces the
required fields before anything is sent.

Local state (all gitignored, all under `runtime/`):

- SQLite: `runtime/dispatch.db`
- Model artifact: `runtime/local_model.json` (plain JSON, not a pickle)
- Audit log: `runtime/audit.jsonl`

Override the location with `RUNTIME_DIR`. To gate the destructive reset route,
set `LOCAL_ADMIN_TOKEN` before starting uvicorn and send it as an
`X-Local-Admin-Token` header.

### Inspect the decision logic without a browser

```bash
python scripts/simulate_scoring.py
```

This runs the real `app/scoring.py` over every fixture scenario and prints the
decision, the margin, the confidence breakdown, and the reason a human is
required. It is the fastest way to see the effect of changing a weight or a
threshold, and it asserts the scoring invariants.

To regenerate the synthetic training fixture:

```bash
python scripts/generate_training_data.py
```

It is deterministic; CI regenerates it and fails the build on any diff.

## 2. AWS prerequisites

Required:

- AWS CLI v2.
- AWS SAM CLI.
- Authenticated profile: `testproject-deploy`.
- AWS permissions for CloudFormation bootstrap and deployment.

Verify:

```bash
aws sts get-caller-identity --profile testproject-deploy
sam --version
```

## 3. Deploy AWS backend

```bash
AWS_PROFILE=testproject-deploy \
bash scripts/deploy.sh testproject-vendor-dispatch us-east-1 dev
```

The script deploys the existing low-cost stack:

- Cognito.
- API Gateway.
- Lambda.
- SQS and DLQ.
- S3 audit bucket.
- IAM and CloudWatch.

It pauses for change-set confirmation. Review replacements before approving.

## 4. Test AWS backend

Read outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name testproject-vendor-dispatch \
  --profile testproject-deploy \
  --region us-east-1 \
  --query "Stacks[0].Outputs" \
  --output table
```

Send a dispatch event:

```bash
python scripts/send_job_created.py \
  --queue-url '<JOB_CREATED_QUEUE_URL>' \
  --file data/job-created.json
```

Receive the recommendation:

```bash
python scripts/receive_recommendation.py \
  --queue-url '<RECOMMENDATION_QUEUE_URL>'
```

The public dashboard uses the live trace workflow instead:

- `POST /runs`: creates a correlation ID and publishes `JobCreated` to SQS.
- `GET /runs/{request_id}`: reads progress from the existing S3 audit bucket.

The dashboard polls the trace for up to 40 seconds and renders each AWS step.
No WebSocket, EventBridge, DynamoDB, or new AWS service is required.

### Authenticated API smoke test

The API uses a Cognito JWT authorizer, so the smoke test signs in as a real
user rather than signing requests with IAM credentials:

```bash
export COGNITO_PASSWORD='...'          # never passed as an argument
python scripts/api_smoke_test.py \
  --api-url '<API_URL>' \
  --user-pool-id '<COGNITO_USER_POOL_ID>' \
  --client-id '<COGNITO_USER_POOL_CLIENT_ID>' \
  --username 'you@example.com' \
  --region us-east-1 \
  --sample-file data/sample.json \
  --test-override
```

It verifies that `GET /health` is reachable anonymously, that
`POST /recommendations` is **not**, that an authenticated recommendation comes
back ranked and explained, and that an override is attributed to the Cognito
subject rather than to the `actor_id` in the request body.

If you already have a token, skip the sign-in with `COGNITO_ID_TOKEN=...`.

## 5. GitHub Pages hybrid deployment

### Create/update the OIDC bootstrap stack

Use the existing provider ARN ending with:

```text
/oidc-provider/token.actions.githubusercontent.com
```

```bash
aws cloudformation deploy \
  --template-file infra/github-oidc-role.yaml \
  --stack-name testproject-github-oidc \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    GitHubOwner='Muhammad-Tahir-Shakil' \
    GitHubOwnerId='30262557' \
    GitHubRepository='testproject' \
    GitHubRepositoryId='1335940410' \
    GitHubEnvironment=github-pages \
    GitHubOidcProviderArn='<EXISTING_GITHUB_OIDC_PROVIDER_ARN>' \
  --region us-east-1 \
  --profile testproject-deploy
```

### GitHub variables

Add this required repository variable:

```text
AWS_DEPLOY_ROLE_ARN=<GitHubActionsRoleArn output>
```

Optional variables have safe defaults:

```text
AWS_REGION=us-east-1
PAGES_ORIGIN=https://<owner>.github.io
PAGES_URL=https://<owner>.github.io/<repository>/
```

Enable GitHub Pages with **GitHub Actions** as the source. Push to `main`.

Workflow:

- `.github/workflows/deploy.yml`

No AWS access keys belong in GitHub variables or secrets.

## 6. Cleanup

Follow:

- [`AWS_RESOURCE_TRACKER.md`](../AWS_RESOURCE_TRACKER.md)
- [`docs/security.md`](security.md)

