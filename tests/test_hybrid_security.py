"""Tests for Cognito actor extraction and PII contract boundaries."""

import pytest
from fastapi import Request
from pydantic import ValidationError

from app.main import authenticated_actor_id
from app.models import Job, RecommendationRequest


def test_authenticated_actor_comes_from_cognito_subject() -> None:
    request = Request(
        {
            "type": "http",
            "aws.event": {
                "requestContext": {
                    "authorizer": {"jwt": {"claims": {"sub": "cognito-sub-1"}}}
                }
            },
        }
    )

    assert authenticated_actor_id(request) == "cognito-sub-1"


def test_missing_cognito_subject_is_not_accepted() -> None:
    request = Request({"type": "http", "aws.event": {}})

    assert authenticated_actor_id(request) is None


def test_job_contract_rejects_unexpected_customer_pii() -> None:
    with pytest.raises(ValidationError):
        Job.model_validate(
            {
                "job_id": "JOB-1",
                "job_type": "repair",
                "region": "north",
                "sla_hours": 8,
                "customer_email": "person@example.com",
            }
        )


def test_recommendation_contract_rejects_unknown_pii_fields() -> None:
    with pytest.raises(ValidationError):
        RecommendationRequest.model_validate(
            {
                "job": {
                    "job_id": "JOB-1",
                    "job_type": "repair",
                    "region": "north",
                    "sla_hours": 8,
                },
                "vendors": [],
                "customer_address": "private",
            }
        )
