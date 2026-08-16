"""Unit tests for ranking, factor calculation, and abstention behavior."""

import json
from pathlib import Path

from app.models import Job, VendorProfile
from app.scoring import inferred_skills, score_vendors


def load_sample() -> tuple[Job, list[VendorProfile]]:
    data_path = Path(__file__).parents[1] / "data" / "sample.json"
    data = json.loads(data_path.read_text(encoding="utf-8"))
    return Job.model_validate(data["job"]), [
        VendorProfile.model_validate(vendor) for vendor in data["vendors"]
    ]


def test_ranks_top_three_and_excludes_full_vendor() -> None:
    job, vendors = load_sample()

    recommendations = score_vendors(job, vendors, top_k=3)

    assert len(recommendations) == 3
    assert [item.rank for item in recommendations] == [1, 2, 3]
    assert recommendations[0].vendor_id == "V-001"
    assert "completion rate" in recommendations[0].rationale
    assert all(item.score_factors.skill_match >= 0 for item in recommendations)
    assert "V-004" not in {item.vendor_id for item in recommendations}


def test_top_k_is_capped_by_available_candidates() -> None:
    job, vendors = load_sample()

    recommendations = score_vendors(job, vendors, top_k=5)

    assert len(recommendations) == 3


def test_inactive_and_unavailable_vendors_are_not_recommended() -> None:
    job, vendors = load_sample()
    vendors[0].active = False
    vendors[1].capacity_used = vendors[1].capacity_total

    recommendations = score_vendors(job, vendors, top_k=5)

    assert [item.vendor_id for item in recommendations] == ["V-003"]


def test_low_quality_candidate_is_marked_for_manual_review() -> None:
    job = Job(
        job_id="JOB-LOW",
        job_type="specialist repair",
        required_skills=["rare-skill"],
        region="south",
        sla_hours=1,
        risk_level="high",
    )
    vendor = VendorProfile(
        vendor_id="V-LOW",
        name="Low Confidence Vendor",
        service_regions=["north"],
        skills=["basic"],
        capacity_total=10,
        capacity_used=9,
        completion_rate=0.1,
        similar_job_rate=0.1,
        avg_response_hours=10,
        rework_rate=0.9,
        risk_levels_supported=["low"],
    )

    recommendations = score_vendors(job, [vendor], top_k=3)

    assert len(recommendations) == 1
    assert recommendations[0].abstained is True
    assert recommendations[0].confidence < 0.45


def test_equal_scores_have_stable_vendor_id_order() -> None:
    job = Job(
        job_id="JOB-TIE",
        job_type="repair",
        region="north",
        sla_hours=8,
    )
    base = {
        "name": "Same Profile",
        "service_regions": ["north"],
        "skills": [],
        "capacity_total": 1,
        "capacity_used": 0,
        "completion_rate": 0.8,
        "similar_job_rate": 0.8,
        "avg_response_hours": 2,
        "rework_rate": 0.1,
    }
    vendors = [
        VendorProfile(vendor_id="V-002", **base),
        VendorProfile(vendor_id="V-001", **base),
    ]

    recommendations = score_vendors(job, vendors, top_k=2)

    assert [item.vendor_id for item in recommendations] == ["V-001", "V-002"]


def test_title_and_details_infer_skills_and_change_ranking() -> None:
    solar_vendor = VendorProfile(
        vendor_id="V-SOLAR",
        name="Solar Storage Specialists",
        service_regions=["south"],
        skills=["battery", "solar", "electrical"],
        capacity_total=5,
        capacity_used=1,
        completion_rate=0.9,
        similar_job_rate=0.9,
        avg_response_hours=4,
        rework_rate=0.05,
        risk_levels_supported=["medium", "high"],
    )
    plumbing_vendor = VendorProfile(
        vendor_id="V-PLUMB",
        name="Water Systems Group",
        service_regions=["south"],
        skills=["plumbing", "water treatment"],
        capacity_total=5,
        capacity_used=1,
        completion_rate=0.9,
        similar_job_rate=0.9,
        avg_response_hours=4,
        rework_rate=0.05,
        risk_levels_supported=["medium", "high"],
    )
    solar_job = Job(
        job_id="JOB-TEXT-1",
        job_type="energy inspection",
        title="Battery storage thermal alarm",
        details="Inspect the solar array and battery controller before returning the site to service.",
        region="south",
        sla_hours=8,
        risk_level="high",
    )
    plumbing_job = solar_job.model_copy(
        update={
            "job_id": "JOB-TEXT-2",
            "title": "Hot water riser leak",
            "details": "Repair the pipework and verify water treatment equipment after the leak.",
            "risk_level": "medium",
        }
    )

    assert set(inferred_skills(solar_job)) == {"battery", "solar", "controls"}
    assert score_vendors(
        solar_job, [solar_vendor, plumbing_vendor], top_k=1
    )[0].vendor_id == "V-SOLAR"
    assert score_vendors(
        plumbing_job, [solar_vendor, plumbing_vendor], top_k=1
    )[0].vendor_id == "V-PLUMB"
