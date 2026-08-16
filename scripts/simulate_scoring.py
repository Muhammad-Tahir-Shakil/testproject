"""Run the real scoring module over every fixture scenario, without pydantic.

``app/scoring.py`` decides whether a human is required, and that behaviour is
easiest to review as a table. This executes the actual module -- it does not
reimplement it -- using lightweight stand-ins for the pydantic models, and
prints the decision for every fixture scenario.

    python scripts/simulate_scoring.py

Exits non-zero on an impossible result: score outside 0-100, confidence outside
0-1, non-contiguous ranks, or a "ready" status still carrying review reasons.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from dataclasses import dataclass, field, fields
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

FEATURE_NAMES = [
    "availability",
    "capacity",
    "skill_match",
    "region_match",
    "completion_rate",
    "similar_job_rate",
    "rework_history",
    "sla_fit",
    "risk_fit",
]


@dataclass
class ScoreFactors:
    availability: float
    capacity: float
    skill_match: float
    region_match: float
    completion_rate: float
    similar_job_rate: float
    rework_history: float
    sla_fit: float
    risk_fit: float

    def model_dump(self) -> dict[str, float]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass
class Job:
    job_id: str
    job_type: str
    region: str
    sla_hours: float
    risk_level: str = "low"
    title: str = ""
    details: str = ""
    required_skills: list[str] = field(default_factory=list)
    customer_name: str = ""
    site_name: str = ""
    asset_label: str = ""


@dataclass
class VendorProfile:
    vendor_id: str
    name: str
    capacity_total: int
    capacity_used: int
    completion_rate: float
    similar_job_rate: float
    avg_response_hours: float
    rework_rate: float
    service_regions: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    risk_levels_supported: list[str] = field(
        default_factory=lambda: ["low", "medium", "high"]
    )
    active: bool = True
    sample_size: int = 0
    data_age_hours: float = 0.0

    @property
    def available_capacity(self) -> int:
        return max(self.capacity_total - self.capacity_used, 0)


@dataclass
class Recommendation:
    vendor_id: str
    vendor_name: str
    rank: int
    score: float
    confidence: float
    abstained: bool
    score_factors: ScoreFactors
    rationale: str
    rule_score: float = 0.0
    model_score: float | None = None
    requirement_fit: float = 0.0
    evidence_strength: float = 0.0


def load_scoring():
    """Import app/scoring.py with a stub `app.models` module."""

    package = types.ModuleType("app")
    package.__path__ = [str(PROJECT_ROOT / "src" / "app")]
    sys.modules["app"] = package

    models = types.ModuleType("app.models")
    models.Job = Job
    models.VendorProfile = VendorProfile
    models.ScoreFactors = ScoreFactors
    models.Recommendation = Recommendation
    sys.modules["app.models"] = models

    spec = importlib.util.spec_from_file_location(
        "app.scoring", PROJECT_ROOT / "src" / "app" / "scoring.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["app.scoring"] = module
    spec.loader.exec_module(module)
    return module


def build_model_predictor():
    """Load the same JSON artifact format the application uses."""

    artifact_path = PROJECT_ROOT / "runtime" / "local_model.json"
    if not artifact_path.exists():
        return None, "none (no runtime/local_model.json; run the local setup first)"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    if artifact.get("feature_names") != FEATURE_NAMES:
        return None, "none (artifact features do not match ScoreFactors)"

    def predict(factors: ScoreFactors) -> float:
        values = factors.model_dump()
        raw = artifact["intercept"] + sum(
            coefficient * values[name]
            for coefficient, name in zip(
                artifact["coefficients"], artifact["feature_names"]
            )
        )
        return min(max(raw, 0.0), 1.0)

    return predict, f"{artifact.get('version')} ({artifact.get('training_rows')} rows)"


def scenarios() -> list[tuple[str, Job, list[VendorProfile], int]]:
    out: list[tuple[str, Job, list[VendorProfile], int]] = []

    sample = json.loads((PROJECT_ROOT / "data" / "sample.json").read_text("utf-8"))
    out.append(
        (
            "data/sample.json",
            Job(**sample["job"]),
            [VendorProfile(**vendor) for vendor in sample["vendors"]],
            sample.get("top_k", 3),
        )
    )

    catalog = json.loads((PROJECT_ROOT / "frontend" / "jobs.json").read_text("utf-8"))
    vendors = [VendorProfile(**vendor) for vendor in catalog["vendors"]]
    for scenario in catalog["scenarios"]:
        out.append(
            (
                f"jobs.json/{scenario['id']}",
                Job(**scenario["job"]),
                vendors,
                scenario.get("top_k", 3),
            )
        )
    return out


def main() -> int:
    scoring = load_scoring()
    predictor, model_label = build_model_predictor()
    problems: list[str] = []

    print(f"model blend: {model_label}")
    print(
        f"thresholds: confidence >= {scoring.CONFIDENCE_THRESHOLD}, "
        f"margin >= {scoring.MARGIN_THRESHOLD}, "
        f"human approval for {sorted(scoring.HUMAN_APPROVAL_RISK_LEVELS)}"
    )
    print()

    for label, job, vendors, top_k in scenarios():
        result = scoring.evaluate(
            job, vendors, top_k=top_k, model_predictor=predictor
        )
        top = result.recommendations[0] if result.recommendations else None
        status = "MANUAL REVIEW" if result.requires_human_decision else "auto-ready"
        margin = (
            "n/a" if result.decision_margin is None else f"{result.decision_margin:6.2f}"
        )
        print(f"{label}  [{job.risk_level} risk]  -> {status}")
        if top:
            print(
                f"    #1 {top.vendor_name:<26} score {top.score:6.2f}  "
                f"margin {margin}  conf {top.confidence:.3f} "
                f"(fit {top.requirement_fit:.2f}, evidence {top.evidence_strength:.2f})"
            )
            for rec in result.recommendations[1:]:
                print(
                    f"    #{rec.rank} {rec.vendor_name:<26} score {rec.score:6.2f}"
                    f"{'':<16}conf {rec.confidence:.3f}"
                )
        for reason in result.review_reasons:
            print(f"    ! {reason}")
        print()

        # Invariants.
        for rec in result.recommendations:
            if not 0 <= rec.score <= 100:
                problems.append(f"{label}: score {rec.score} outside 0-100")
            if not 0 <= rec.confidence <= 1:
                problems.append(f"{label}: confidence {rec.confidence} outside 0-1")
            if rec.abstained != (rec.confidence < scoring.CONFIDENCE_THRESHOLD):
                problems.append(f"{label}: abstained flag disagrees with threshold")
        ranks = [rec.rank for rec in result.recommendations]
        if ranks != list(range(1, len(ranks) + 1)):
            problems.append(f"{label}: ranks are not contiguous: {ranks}")
        if len(ranks) > top_k:
            problems.append(f"{label}: returned {len(ranks)} results for top_k={top_k}")
        if not result.requires_human_decision and result.review_reasons:
            problems.append(f"{label}: auto-ready but carries review reasons")
        scores = [rec.score for rec in result.recommendations]
        if scores != sorted(scores, reverse=True):
            problems.append(f"{label}: results are not ordered by score: {scores}")

    print("-" * 62)
    if problems:
        print(f"FAILED: {len(problems)} invariant violation(s)")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("PASSED: all scenarios satisfy the scoring invariants")
    return 0


if __name__ == "__main__":
    sys.exit(main())
