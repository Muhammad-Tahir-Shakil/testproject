#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   bash scripts/deploy.sh <stack-name> <region> <environment> [frontend-origin] [frontend-redirect-uri]

STACK_NAME="${1:-testproject-vendor-dispatch}"
REGION="${2:-us-east-1}"
ENVIRONMENT="${3:-dev}"
FRONTEND_ORIGIN="${4:-http://127.0.0.1:8000}"
FRONTEND_REDIRECT_URI="${5:-${FRONTEND_ORIGIN}/}"

command -v aws >/dev/null || {
  echo "AWS CLI is required."
  exit 1
}
command -v sam >/dev/null || {
  echo "AWS SAM CLI is required: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html"
  exit 1
}

sam build
sam deploy \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --capabilities CAPABILITY_IAM \
  --resolve-s3 \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    EnvironmentName="$ENVIRONMENT" \
    FrontendOrigin="$FRONTEND_ORIGIN" \
    FrontendRedirectUri="$FRONTEND_REDIRECT_URI"

echo
echo "Deployment complete. Outputs:"
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query "Stacks[0].Outputs" \
  --output table
