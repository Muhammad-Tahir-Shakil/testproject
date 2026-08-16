# AWS Resource, Cleanup, and Security Tracker

## Deployment recorded

- Stack: `testproject-vendor-dispatch`
- Region: `us-east-1`
- Deployment method: AWS SAM + CloudFormation
- Status: deployed successfully
- Build: successful
- SAM template validation: successful
- Tests before deployment: 18 passed

The AWS account ID, access keys, and secret values are intentionally not
stored in this document.

The IAM API authorization, encrypted SQS, TLS-only S3 policy, and bounded log
retention are implemented in the current template and require one stack
update to become active in AWS.

## Hardened redeploy behavior

Redeploying with the same stack name updates the existing CloudFormation
stack; it does not create a second application stack.

Expected in-place updates:

- Existing S3 audit bucket.
- Existing SQS input, output, and DLQ queues.
- Existing API Gateway API and stage.
- Existing IAM roles and permissions.

Expected additional or replacement resources:

- Two explicitly managed CloudWatch log groups.
- The two Lambda functions may be replaced because the hardened template gives
  them explicit names. CloudFormation will show this in the change set before
  execution.

The SAM deployment bucket is reused when deploying from the same profile,
region, and SAM configuration. It is separate from the application stack.

The hybrid template additionally adds one Cognito User Pool, one public client,
and one user-pool domain during its first hybrid redeploy. Cognito adds no
AWS access keys to the browser.

The optional GitHub Actions bootstrap stack is separate:

- Stack: `testproject-github-oidc`
- Existing GitHub OIDC provider reference.
- Repository/branch-restricted deployment role.
- Restricted CloudFormation service role used by GitHub Actions.
- The service role owns SAM change-set transform and application resource
  provisioning; the OIDC role only assumes it and uploads artifacts.

Delete that bootstrap stack only after disabling the GitHub Actions workflow:

```bash
aws cloudformation delete-stack \
  --stack-name testproject-github-oidc \
  --region "$AWS_REGION"
```

## Legacy resource cleanup audit

Based on the previous stack output, there is no second application stack and
no obsolete application queue. CloudFormation owns the previous logical
resources and will update or replace them during the hardened deployment.

Check these possible leftovers after redeployment:

1. Old Lambda functions created with generated physical names.
2. Old Lambda log groups created automatically before explicit log groups were
   added.
3. The separate SAM deployment artifact bucket.

List Lambda functions related to this stack:

```bash
aws lambda list-functions \
  --region "$AWS_REGION" \
  --query "Functions[?contains(FunctionName, 'testproject')].FunctionName" \
  --output table
```

The expected active names after hardening are:

```text
testproject-dev-worker
testproject-dev-api
```

If an older generated Lambda remains after the CloudFormation update, confirm
it is not referenced by the stack before deleting it:

```bash
aws cloudformation describe-stack-resources \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query "StackResources[?ResourceType=='AWS::Lambda::Function']"
```

List Lambda log groups:

```bash
aws logs describe-log-groups \
  --region "$AWS_REGION" \
  --log-group-name-prefix /aws/lambda/testproject \
  --query "logGroups[].logGroupName" \
  --output table
```

Keep the two hardened log groups:

```text
/aws/lambda/testproject-dev-worker
/aws/lambda/testproject-dev-api
```

Delete only an old generated log group after confirming it is not active:

```bash
aws logs delete-log-group \
  --log-group-name "<OLD_LOG_GROUP_NAME>" \
  --region "$AWS_REGION"
```

The SAM deployment bucket is not an application resource. Inspect it before
deleting:

```bash
aws s3 ls "s3://$SAM_BUCKET" --region "$AWS_REGION"
```

Delete it only if no other SAM stack or project uses it. Do not delete it as
part of normal application cleanup.

## Resources created by the CloudFormation stack

CloudFormation owns these resources and should delete them when the stack is
deleted:

- `AuditBucket`
  - S3 audit bucket.
  - Encrypted with S3-managed AES-256 encryption.
  - Retained by template policy, so it needs manual cleanup.
  - This is the current application audit bucket, not an obsolete bucket.
  - It is intentionally kept when the stack is deleted to protect audit data.
- `JobCreatedQueue`
  - SQS input queue.
  - Receives dispatch `JobCreated` events.
- `JobCreatedDeadLetterQueue`
  - SQS DLQ for repeatedly failing input messages.
- `RecommendationQueue`
  - SQS output queue.
  - Receives `VendorRecommendationGenerated` events.
- `RecommendationWorker`
  - Lambda SQS worker.
  - Validates events, scores vendors, publishes results, and audits output.
- `RecommendationApi`
  - Lambda adapter for the FastAPI API.
- `ServerlessHttpApi`
  - API Gateway HTTP API.
- API Gateway default stage.
- Two Lambda execution IAM roles.
- Two Lambda permission resources for API Gateway.
- One SQS-to-Lambda event source mapping.
- Two CloudWatch log groups with 14-day retention.

No EventBridge, DynamoDB, Step Functions, SageMaker, NAT Gateway, or VPC was
created.

## SAM-managed deployment bucket

SAM also created this deployment artifact bucket outside the application stack:

```text
aws-sam-cli-managed-default-samclisourcebucket-qtt8gb2blgzq
```

Do not delete this bucket until confirming that no other SAM application uses
it. It stores deployment packages, not application audit data.

## Current public exposure

API Gateway has a public HTTPS hostname. Authorization is a **Cognito JWT
authorizer**: every route requires an ID token issued by the user pool whose
audience matches the app client. Anonymous requests are rejected by API Gateway
before the Lambda is invoked.

`GET /health` is the single deliberate exception and is reachable without a
token. It returns only the model version and the confidence and margin
thresholds — no customer data, no resource identifiers, and nothing that would
assist an attacker. It exists so an uptime monitor and the post-deployment smoke
test can confirm the stack is alive without minting a user token.

Use `scripts/api_smoke_test.py` to exercise the authenticated routes; it
exchanges Cognito credentials for a token and calls the API with it. Do not send
real customer PII to the endpoint.

The following resources are not public internet endpoints:

- S3 audit bucket.
- SQS queues.
- Lambda functions.
- IAM roles.
- CloudWatch logs.

S3 public access is blocked in `template.yaml`. SQS and S3 operations require
signed AWS IAM requests. Lambda functions have no public inbound endpoint.
SQS queues use SQS-managed server-side encryption, and S3 denies requests that
do not use TLS.

## IAM permissions

### Recommendation worker role

The worker role can:

- Receive messages from `JobCreatedQueue`.
- Delete successfully processed messages.
- Read SQS queue attributes.
- Send messages to `RecommendationQueue`.
- Put objects in the audit bucket.
- Write only to its own CloudWatch log group.

### Recommendation API role

The API role can:

- Put audit objects in the audit bucket.
- Write only to its own CloudWatch log group.

The template does not grant wildcard access to all S3 buckets or all SQS
queues. `CAPABILITY_IAM` is required because SAM creates these roles.

## Security checklist

- [x] No credentials are embedded in source code.
- [x] No credentials are included in Lambda environment variables.
- [x] S3 public access is blocked.
- [x] S3 server-side encryption is enabled.
- [x] S3 insecure transport is denied.
- [x] SQS is accessed through IAM-signed requests.
- [x] SQS server-side encryption is enabled.
- [x] Lambda access is role-based.
- [x] API Gateway routes require IAM authorization.
- [x] Hybrid template defines Cognito JWT authorization.
- [x] Lambda log groups have bounded 14-day retention.
- [x] Failed SQS messages go to a DLQ.
- [x] Audit records redact common PII keys.
- [x] No VPC/NAT resources were added unnecessarily.
- [ ] Add API throttling/WAF before public production use.
- [ ] Replace broad deployment-user permissions with a reviewed policy.
- [ ] Use temporary credentials or SSO instead of long-lived access keys.
- [ ] Rotate any credential that has been exposed.

## Inspect deployed resources

Set the profile and region:

```bash
export AWS_PROFILE=testproject-deploy
export AWS_REGION=us-east-1
export STACK_NAME=testproject-vendor-dispatch
```

List stack resources:

```bash
aws cloudformation describe-stack-resources \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION"
```

List stack outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query "Stacks[0].Outputs" \
  --output table
```

Check S3 public access protection:

```bash
aws s3api get-public-access-block \
  --bucket testproject-vendor-dispatch-auditbucket-gdkrqmxxnkux \
  --region "$AWS_REGION"
```

List application queues:

```bash
aws sqs list-queues \
  --queue-name-prefix testproject-dev- \
  --region "$AWS_REGION"
```

## Cleanup after the assessment

Capture the retained audit bucket name before deleting the stack:

```bash
export AUDIT_BUCKET="$(
  aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='AuditBucketName'].OutputValue" \
    --output text
)"
```

Delete all CloudFormation-owned application resources:

```bash
aws cloudformation delete-stack \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION"
```

Wait for deletion:

```bash
aws cloudformation wait stack-delete-complete \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION"
```

The audit bucket is intentionally retained. Delete it only after reviewing
the audit data:

```bash
aws s3 rm "s3://$AUDIT_BUCKET" \
  --recursive \
  --region "$AWS_REGION"
```

```bash
aws s3api delete-bucket \
  --bucket "$AUDIT_BUCKET" \
  --region "$AWS_REGION"
```

The SAM deployment bucket is separate. Delete it only if it is not shared:

```bash
export SAM_BUCKET=aws-sam-cli-managed-default-samclisourcebucket-qtt8gb2blgzq
aws s3 ls "s3://$SAM_BUCKET" --region "$AWS_REGION"
```

If it is dedicated to this project:

```bash
aws s3 rm "s3://$SAM_BUCKET" \
  --recursive \
  --region "$AWS_REGION"
```

```bash
aws s3api delete-bucket \
  --bucket "$SAM_BUCKET" \
  --region "$AWS_REGION"
```

## Post-cleanup verification

```bash
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION"
```

The command should return a stack-not-found error.

Also verify that no application queues remain:

```bash
aws sqs list-queues \
  --queue-name-prefix testproject-dev- \
  --region "$AWS_REGION"
```

Keep this document with the project so resource ownership and cleanup steps
remain clear before the AWS account is reused.
