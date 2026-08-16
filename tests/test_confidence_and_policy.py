"""Tests for the authority model: confidence, abstention, and review policy.

The central property under test is that **confidence is not a restatement of
the score**. The earlier implementation averaged the factors and blended in the
score, which made a low confidence structurally impossible for a high-scoring
vendor, so abstention almost never fired. These tests pin the separation.
"""

import pytest

from app.models import Job, VendorProfile
from app.scoring import (
    CONFIDENCE_THRESHOLD,
    MARGIN_THRESHOLD,
    evaluate,
    evidence_strength,
    requirement_fit,
)


def job(**overrides) -> Job:
    values = {
        "job_id": "JOB-CONF",
        "job_type": "repair",
        "region": "north",
        "sla_hours": 8,
        "risk_level": "low",
        "required_skills": ["hvac"],
    }
    values.update(overrides)
    return Job(**values)


def vendor(vendor_id: str = "V-1", **overrides) -> VendorProfile:
    values = {
        "vendor_id": vendor_id,
        "name": f"Vendor {vendor_id}",
        "service_regions": ["north"],
        "skills": ["hvac"],
        "capacity_total": 10,
        "capacity_used": 2,
        "completion_rate": 0.95,
        "similar_job_rate": 0.9,
        "avg_response_hours": 2.0,
        "rework_rate": 0.05,
        "risk_levels_supported": ["low", "medium", "high"],
        "sample_size": 200,
        "data_age_hours": 2.0,
    }
    values.update(overrides)
    return VendorProfile(**values)


# --- requirement fit -------------------------------------------------------


def test_requirement_fit_is_the_weakest_gate_not_the_average() -> None:
    """Perfect on three gates and absent on the fourth is unsuitable, not 75%."""

    result = evaluate(job(), [vendor(skills=["plumbing"])], top_k=1)
    top = result.recommendations[0]

    assert top.requirement_fit == 0.0
    assert top.confidence < CONFIDENCE_THRESHOLD
    assert top.abstained is True


def test_requirement_fit_is_limited_by_region_when_skills_are_perfect() -> None:
    result = evaluate(job(), [vendor(service_regions=["south"])], top_k=1)

    assert result.recommendations[0].requirement_fit == pytest.approx(0.2)


# --- evidence strength -----------------------------------------------------


def test_more_history_is_stronger_evidence() -> None:
    thin = evidence_strength(vendor(sample_size=3, data_age_hours=0))
    thick = evidence_strength(vendor(sample_size=400, data_age_hours=0))

    assert thin < thick


def test_stale_data_weakens_evidence() -> None:
    fresh = evidence_strength(vendor(sample_size=200, data_age_hours=1))
    stale = evidence_strength(vendor(sample_size=200, data_age_hours=720))

    assert stale < fresh


def test_unknown_provenance_is_treated_as_weak_not_perfect() -> None:
    """An unverified profile must bias toward review, not away from it."""

    unknown = evidence_strength(vendor(sample_size=0, data_age_hours=0))
    known = evidence_strength(vendor(sample_size=400, data_age_hours=0))

    assert 0 < unknown < known


def test_a_high_scoring_vendor_can_still_have_low_confidence() -> None:
    """The regression this whole rework exists to prevent."""

    thin_history = vendor(
        completion_rate=1.0,
        similar_job_rate=1.0,
        rework_rate=0.0,
        sample_size=2,
        data_age_hours=400,
    )
    result = evaluate(job(), [thin_history], top_k=1)
    top = result.recommendations[0]

    assert top.score > 85
    assert top.confidence < CONFIDENCE_THRESHOLD
    assert top.abstained is True
    assert any("evidence" in reason for reason in result.review_reasons)


# --- policy ----------------------------------------------------------------


def test_high_risk_never_auto_dispatches_however_good_the_candidate() -> None:
    result = evaluate(job(risk_level="high"), [vendor(), vendor("V-2")], top_k=2)

    assert result.recommendations[0].confidence > CONFIDENCE_THRESHOLD
    assert result.requires_human_decision is True
    assert any("high-risk" in reason for reason in result.review_reasons)


def test_low_risk_with_strong_evidence_is_auto_ready() -> None:
    result = evaluate(
        job(),
        [vendor("V-1"), vendor("V-2", completion_rate=0.6, similar_job_rate=0.5)],
        top_k=2,
    )

    assert result.requires_human_decision is False
    assert result.review_reasons == []


def test_a_close_call_requires_a_human() -> None:
    """Two near-identical candidates is a tie broken on noise, not a decision."""

    result = evaluate(job(), [vendor("V-1"), vendor("V-2")], top_k=2)

    assert result.decision_margin is not None
    assert result.decision_margin < MARGIN_THRESHOLD
    assert result.requires_human_decision is True
    assert any("Close call" in reason for reason in result.review_reasons)


def test_no_eligible_vendor_reports_a_specific_reason() -> None:
    result = evaluate(job(), [vendor(active=False)], top_k=3)

    assert result.recommendations == []
    assert result.decision_margin is None
    assert result.review_reasons == [
        "No eligible vendor has capacity for this job."
    ]


def test_margin_is_computed_over_all_eligible_vendors_not_just_top_k() -> None:
    """top_k=1 must still report a real margin."""

    result = evaluate(
        job(),
        [vendor("V-1"), vendor("V-2", completion_rate=0.5, similar_job_rate=0.4)],
        top_k=1,
    )

    assert len(result.recommendations) == 1
    assert result.decision_margin is not None
    assert result.decision_margin > 0


# --- model blend -----------------------------------------------------------


def test_model_blend_moves_the_score_within_its_bounded_weight() -> None:
    candidates = [vendor()]
    rules_only = evaluate(job(), candidates, top_k=1).recommendations[0]
    blended = evaluate(
        job(), candidates, top_k=1, model_predictor=lambda factors: 0.0, model_weight=0.2
    ).recommendations[0]

    assert blended.rule_score == pytest.approx(rules_only.score)
    assert blended.model_score == 0.0
    # A model output of 0 can pull the score down by at most model_weight.
    assert blended.score == pytest.approx(rules_only.score * 0.8, abs=0.01)


def test_model_weight_is_clamped_to_the_unit_interval() -> None:
    result = evaluate(
        job(),
        [vendor()],
        top_k=1,
        model_predictor=lambda factors: 0.0,
        model_weight=5.0,
    )

    assert result.recommendations[0].score == pytest.approx(0.0)


def test_model_output_is_clamped_before_blending() -> None:
    result = evaluate(
        job(),
        [vendor()],
        top_k=1,
        model_predictor=lambda factors: 99.0,
        model_weight=1.0,
    )

    assert result.recommendations[0].model_score == 100.0
    assert result.recommendations[0].score == 100.0


def test_the_model_cannot_promote_an_ineligible_vendor() -> None:
    """The eligibility gate runs before the model is ever consulted."""

    result = evaluate(
        job(),
        [vendor("V-FULL", capacity_used=10), vendor("V-OK")],
        top_k=3,
        model_predictor=lambda factors: 1.0,
    )

    assert [item.vendor_id for item in result.recommendations] == ["V-OK"]


def test_confidence_ignores_the_model_entirely() -> None:
    """Authority must not be purchasable with a confident model output."""

    candidates = [vendor(skills=["plumbing"])]
    without = evaluate(job(), candidates, top_k=1).recommendations[0]
    with_model = evaluate(
        job(), candidates, top_k=1, model_predictor=lambda factors: 1.0
    ).recommendations[0]

    assert with_model.confidence == without.confidence
    assert with_model.abstained is True
