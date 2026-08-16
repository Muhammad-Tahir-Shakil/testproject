"""Tests for the closed-form ridge fit and the JSON model artifact.

The artifact format is the seam that makes local training deployable, so these
tests pin both the numerics and the failure behaviour. See
docs/platform-decisions.md.
"""

import json
from pathlib import Path

import pytest

from app.local_ml import (
    ARTIFACT_SCHEMA,
    FEATURE_NAMES,
    LocalModel,
    ModelArtifactError,
    fit_ridge,
    load_predictor,
    predict_from_artifact,
    validate_artifact,
)
from app.models import ScoreFactors

TRAINING_PATH = Path(__file__).parents[1] / "data" / "training.json"


def factors(**overrides) -> ScoreFactors:
    values = {name: 0.5 for name in FEATURE_NAMES}
    values.update(overrides)
    return ScoreFactors(**values)


def test_ridge_recovers_a_known_linear_relationship() -> None:
    # y = 0.2*a + 0.5*b + 0.1 exactly; ridge with a tiny penalty should land
    # close to those coefficients rather than merely fitting the data.
    rows = [[a / 10, b / 10] for a in range(11) for b in range(11)]
    outcomes = [0.2 * a + 0.5 * b + 0.1 for a, b in rows]

    coefficients, intercept = fit_ridge(rows, outcomes, alpha=1e-6)

    assert coefficients[0] == pytest.approx(0.2, abs=1e-3)
    assert coefficients[1] == pytest.approx(0.5, abs=1e-3)
    assert intercept == pytest.approx(0.1, abs=1e-3)


def test_ridge_shrinks_a_constant_feature_to_zero() -> None:
    """A constant column carries no information; the intercept should absorb it.

    `availability` is constant in the training fixture because the eligibility
    gate filters unavailable vendors before the model is consulted. This asserts
    that the fixture's design choice produces the expected coefficient rather
    than a spurious one.
    """

    rows = [[1.0, value / 20] for value in range(21)]
    outcomes = [0.3 * (value / 20) + 0.4 for value in range(21)]

    coefficients, intercept = fit_ridge(rows, outcomes, alpha=1.0)

    assert coefficients[0] == pytest.approx(0.0, abs=1e-9)
    assert intercept == pytest.approx(0.4, abs=0.05)


def test_ridge_rejects_an_empty_or_ragged_training_set() -> None:
    with pytest.raises(ModelArtifactError):
        fit_ridge([], [])
    with pytest.raises(ModelArtifactError):
        fit_ridge([[1.0, 2.0], [1.0]], [0.5, 0.6])


def test_training_writes_a_json_artifact_that_round_trips(tmp_path: Path) -> None:
    model = LocalModel(tmp_path / "model.json")

    metadata = model.train(TRAINING_PATH)

    assert metadata["ready"] is True
    assert metadata["training_data_kind"] == "synthetic-fixture"
    artifact = json.loads((tmp_path / "model.json").read_text(encoding="utf-8"))
    assert artifact["schema"] == ARTIFACT_SCHEMA
    assert artifact["feature_names"] == FEATURE_NAMES
    assert len(artifact["coefficients"]) == len(FEATURE_NAMES)

    # A fresh instance must load what the first one wrote.
    reloaded = LocalModel(tmp_path / "model.json")
    assert reloaded.load() is True
    assert reloaded.predict(factors()) == pytest.approx(model.predict(factors()))


def test_predictions_are_clamped_to_the_unit_range() -> None:
    artifact = {
        "schema": ARTIFACT_SCHEMA,
        "version": "test",
        "feature_names": FEATURE_NAMES,
        "coefficients": [100.0] + [0.0] * (len(FEATURE_NAMES) - 1),
        "intercept": -50.0,
    }
    assert predict_from_artifact(artifact, factors(availability=1.0)) == 1.0
    assert predict_from_artifact(artifact, factors(availability=0.0)) == 0.0


def test_missing_artifact_loads_as_not_ready(tmp_path: Path) -> None:
    model = LocalModel(tmp_path / "absent.json")

    assert model.load() is False
    assert model.ready is False
    assert model.load_error is None
    with pytest.raises(ModelArtifactError):
        model.predict(factors())


def test_corrupt_artifact_is_reported_not_raised(tmp_path: Path) -> None:
    """A bad artifact must not make every dashboard route return 500.

    This was a real failure mode: `load()` raised, `LocalWorkflow.__init__` did
    not catch it, and the only recovery was deleting the file by hand because
    even the reset endpoint needed the workflow to construct first.
    """

    path = tmp_path / "model.json"
    path.write_text("{ not json", encoding="utf-8")
    model = LocalModel(path)

    assert model.load() is False
    assert model.ready is False
    assert model.load_error is not None
    assert model.metadata()["load_error"] is not None


def test_artifact_with_wrong_features_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "model.json"
    path.write_text(
        json.dumps(
            {
                "schema": ARTIFACT_SCHEMA,
                "version": "test",
                "feature_names": ["only_one"],
                "coefficients": [1.0],
                "intercept": 0.0,
            }
        ),
        encoding="utf-8",
    )
    model = LocalModel(path)

    assert model.load() is False
    assert "features" in (model.load_error or "")

    with pytest.raises(ModelArtifactError):
        load_predictor(path)


def test_unknown_schema_is_rejected() -> None:
    with pytest.raises(ModelArtifactError):
        validate_artifact(
            {
                "schema": "something-else/9",
                "feature_names": FEATURE_NAMES,
                "coefficients": [0.0] * len(FEATURE_NAMES),
                "intercept": 0.0,
            }
        )


def test_training_refuses_a_fixture_smaller_than_the_feature_count(
    tmp_path: Path,
) -> None:
    """Fewer rows than features cannot constrain the fit.

    Ridge would still return an answer, which is exactly the problem: it would
    be memorising rather than learning. Failing loudly is the point.
    """

    fixture = tmp_path / "tiny.json"
    fixture.write_text(
        json.dumps(
            {
                "feature_names": FEATURE_NAMES,
                "examples": [
                    {"factors": [0.5] * len(FEATURE_NAMES), "outcome": 0.5}
                ]
                * 3,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ModelArtifactError, match="at least"):
        LocalModel(tmp_path / "model.json").train(fixture)


def test_load_predictor_matches_the_local_model(tmp_path: Path) -> None:
    """The Lambda path and the dashboard path must score identically."""

    model = LocalModel(tmp_path / "model.json")
    model.train(TRAINING_PATH)

    predictor = load_predictor(tmp_path / "model.json")

    sample = factors(skill_match=1.0, region_match=1.0, completion_rate=0.9)
    assert predictor(sample) == pytest.approx(model.predict(sample))
