"""Generate the synthetic training fixture for the local hybrid model.

Exists so the fixture is reproducible and its assumptions reviewable rather
than hand-typed. Labels come from a declared ground-truth function plus noise,
so the fit can only rediscover that function: this demonstrates the training ->
artifact -> blended-scoring pipeline, not predictive power on real vendors.

Deterministic -- CI regenerates it and fails on any diff.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = PROJECT_ROOT / "data" / "training.json"

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

# Declared ground truth. Chosen to differ from the rules weights in
# app/scoring.py on purpose: if the model simply reproduced the rule weights,
# blending it in would be a no-op and would prove nothing about the seam.
# Here the "observed" outcome leans harder on skill match, similar-job history
# and rework than the hand-tuned rules do.
GROUND_TRUTH = {
    "availability": 0.06,
    "capacity": 0.04,
    "skill_match": 0.26,
    "region_match": 0.10,
    "completion_rate": 0.14,
    "similar_job_rate": 0.16,
    "rework_history": 0.14,
    "sla_fit": 0.06,
    "risk_fit": 0.04,
}

SEED = 20260816
ROWS = 240
NOISE_SD = 0.04


def _sample_row(rng: random.Random) -> list[float]:
    """Draw one plausible ScoreFactors vector for an *eligible* vendor.

    Availability is pinned to 1.0 on purpose. ``app/scoring.py`` filters
    inactive and at-capacity vendors out before scoring, so the model is only
    ever asked about vendors that already passed that gate. Training on
    unavailable vendors would be train/serve skew: it would teach the model a
    population it never sees at inference, and the fitted availability
    coefficient would absorb a discontinuity that the eligibility filter has
    already handled. The column is constant, so ridge shrinks its coefficient
    to zero and the intercept absorbs the level -- which is the correct
    outcome, and is asserted in tests/test_local_ml.py.

    The other marginals are shaped to look like the real factor pipeline
    rather than uniform noise: region_match is bimodal because app/scoring.py
    emits either 1.0 or the flat out-of-region penalty, and the performance
    rates cluster high because most active vendors are competent.
    """

    availability = 1.0
    capacity = round(min(1.0, max(0.05, rng.betavariate(2.0, 2.0))), 4)
    skill_match = round(rng.choice([0.0, 0.25, 0.33, 0.5, 0.67, 0.75, 1.0]), 4)
    region_match = 1.0 if rng.random() < 0.7 else 0.2
    completion_rate = round(min(1.0, rng.betavariate(9.0, 1.6)), 4)
    similar_job_rate = round(min(1.0, rng.betavariate(7.0, 2.2)), 4)
    rework_history = round(min(1.0, rng.betavariate(9.0, 1.3)), 4)
    sla_fit = round(rng.choice([1.0, 1.0, 1.0, 0.8, 0.55, 0.3, 0.0]), 4)
    risk_fit = 1.0 if rng.random() < 0.8 else 0.25
    return [
        availability,
        capacity,
        skill_match,
        region_match,
        completion_rate,
        similar_job_rate,
        rework_history,
        sla_fit,
        risk_fit,
    ]


def _outcome(row: list[float], rng: random.Random) -> float:
    """Label a row from the declared linear ground truth plus Gaussian noise."""

    base = sum(value * GROUND_TRUTH[name] for value, name in zip(row, FEATURE_NAMES))
    return round(min(1.0, max(0.0, base + rng.gauss(0.0, NOISE_SD))), 4)


def build() -> dict:
    rng = random.Random(SEED)
    examples = []
    for _ in range(ROWS):
        row = _sample_row(rng)
        examples.append({"factors": row, "outcome": _outcome(row, rng)})
    return {
        "description": (
            "Synthetic training fixture for the local hybrid model. Labels are "
            "generated from a declared ground-truth function plus Gaussian "
            "noise; they are not real customer outcomes."
        ),
        "generator": "scripts/generate_training_data.py",
        "seed": SEED,
        "rows": ROWS,
        "noise_sd": NOISE_SD,
        "ground_truth_weights": GROUND_TRUTH,
        "alpha": 1.0,
        "feature_names": FEATURE_NAMES,
        "examples": examples,
    }


def main() -> None:
    payload = build()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(payload['examples'])} examples to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
