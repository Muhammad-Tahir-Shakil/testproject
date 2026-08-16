"""Offline ridge-regression model for the hybrid scorer.

Three constraints, all decisions rather than limitations (reasoning in
``docs/platform-decisions.md``):

1. **Trained locally.** No managed endpoint is provisioned -- a cost decision.
2. **No pickle.** The artifact is plain JSON. Unpickling a model file is
   arbitrary code execution, a linear model has nothing that needs pickling,
   and JSON is reviewable in a pull request and diffable across versions.
3. **No third-party dependency.** The fit is closed-form, so ~40 lines of
   standard library replace a 90 MB scientific stack. The same predictor
   therefore runs unchanged locally and in Lambda: the model is
   deployment-ready, and deploying it is a toggle, not a rewrite.

Trained on synthetic fixture data. Must not be described as trained on real
customer outcomes.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .models import ScoreFactors


MODEL_VERSION = "hybrid-local-v1"
ARTIFACT_SCHEMA = "vendor-score-linear/1"
FEATURE_NAMES = list(ScoreFactors.model_fields)
DEFAULT_ALPHA = 1.0


class ModelArtifactError(RuntimeError):
    """Raised when an artifact is missing, malformed, or feature-mismatched."""


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve ``matrix @ x = vector`` by Gauss-Jordan with partial pivoting.

    The system is (features x features), so at nine features this is trivial
    to compute and easy to verify by hand against a reference implementation.
    """

    size = len(vector)
    augmented = [list(matrix[row]) + [vector[row]] for row in range(size)]

    for column in range(size):
        pivot_row = max(
            range(column, size), key=lambda row: abs(augmented[row][column])
        )
        if abs(augmented[pivot_row][column]) < 1e-12:
            raise ModelArtifactError(
                "Training matrix is singular; increase alpha or add examples."
            )
        augmented[column], augmented[pivot_row] = (
            augmented[pivot_row],
            augmented[column],
        )
        pivot = augmented[column][column]
        augmented[column] = [value / pivot for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0.0:
                continue
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column])
            ]

    return [augmented[row][size] for row in range(size)]


def fit_ridge(
    features: Sequence[Sequence[float]],
    outcomes: Sequence[float],
    alpha: float = DEFAULT_ALPHA,
) -> tuple[list[float], float]:
    """Return ``(coefficients, intercept)`` for a centred ridge fit.

    Centring the design matrix keeps the intercept out of the penalty, which
    is the same convention scikit-learn's ``Ridge(fit_intercept=True)`` uses.
    The penalty is normalized by row count so it remains stable as the
    synthetic training fixture grows.
    """

    rows = len(outcomes)
    if rows == 0:
        raise ModelArtifactError("Training set is empty.")
    width = len(features[0])
    if any(len(row) != width for row in features):
        raise ModelArtifactError("Training rows have inconsistent feature counts.")

    feature_means = [
        sum(row[column] for row in features) / rows for column in range(width)
    ]
    outcome_mean = sum(outcomes) / rows
    centred = [
        [row[column] - feature_means[column] for column in range(width)]
        for row in features
    ]
    centred_outcomes = [value - outcome_mean for value in outcomes]

    normalized_alpha = alpha / rows
    gram = [
        [
            sum(centred[row][left] * centred[row][right] for row in range(rows))
            + (normalized_alpha if left == right else 0.0)
            for right in range(width)
        ]
        for left in range(width)
    ]
    moment = [
        sum(centred[row][column] * centred_outcomes[row] for row in range(rows))
        for column in range(width)
    ]

    coefficients = _solve(gram, moment)
    intercept = outcome_mean - sum(
        coefficient * mean for coefficient, mean in zip(coefficients, feature_means)
    )
    return coefficients, intercept


def predict_from_artifact(artifact: dict[str, Any], factors: ScoreFactors) -> float:
    """Score one candidate from a loaded artifact, clamped to the unit range."""

    values = factors.model_dump()
    raw = artifact["intercept"] + sum(
        coefficient * values[name]
        for coefficient, name in zip(artifact["coefficients"], artifact["feature_names"])
    )
    return min(max(raw, 0.0), 1.0)


def validate_artifact(artifact: Any) -> dict[str, Any]:
    """Reject anything that is not a well-formed artifact for these features."""

    if not isinstance(artifact, dict):
        raise ModelArtifactError("Model artifact must be a JSON object.")
    if artifact.get("schema") != ARTIFACT_SCHEMA:
        raise ModelArtifactError(
            f"Unsupported artifact schema {artifact.get('schema')!r}; "
            f"expected {ARTIFACT_SCHEMA!r}."
        )
    if artifact.get("feature_names") != FEATURE_NAMES:
        raise ModelArtifactError(
            "Artifact features do not match ScoreFactors; retrain the model."
        )
    coefficients = artifact.get("coefficients")
    if not isinstance(coefficients, list) or len(coefficients) != len(FEATURE_NAMES):
        raise ModelArtifactError("Artifact coefficients are missing or malformed.")
    if not all(isinstance(value, (int, float)) for value in coefficients):
        raise ModelArtifactError("Artifact coefficients must be numeric.")
    if not isinstance(artifact.get("intercept"), (int, float)):
        raise ModelArtifactError("Artifact intercept must be numeric.")
    return artifact


def load_predictor(path: Path) -> Callable[[ScoreFactors], float]:
    """Return a predictor bound to a validated artifact on disk.

    Used by both the local dashboard and, when ``SCORING_MODEL_ARTIFACT`` is
    set, the AWS handlers -- the same code path in both places.
    """

    artifact = validate_artifact(json.loads(Path(path).read_text(encoding="utf-8")))
    return lambda factors: predict_from_artifact(artifact, factors)


class LocalModel:
    """Train, persist, and use the bounded local regression model."""

    def __init__(self, artifact_path: Path) -> None:
        self.artifact_path = Path(artifact_path)
        self._artifact: dict[str, Any] | None = None
        self.trained_at: str | None = None
        self.load_error: str | None = None

    @property
    def ready(self) -> bool:
        return self._artifact is not None

    @property
    def version(self) -> str:
        if self._artifact is None:
            return MODEL_VERSION
        return str(self._artifact.get("version", MODEL_VERSION))

    def train(self, training_path: Path) -> dict[str, Any]:
        data = json.loads(Path(training_path).read_text(encoding="utf-8"))
        feature_names = data.get("feature_names", FEATURE_NAMES)
        if feature_names != FEATURE_NAMES:
            raise ModelArtifactError("Training features do not match ScoreFactors")
        examples = data.get("examples") or []
        if len(examples) < len(FEATURE_NAMES):
            # Fewer rows than features cannot constrain the fit. Ridge will
            # still return an answer, which is exactly the problem: it would be
            # memorising, not learning. Fail loudly instead.
            raise ModelArtifactError(
                f"Need at least {len(FEATURE_NAMES)} training examples, "
                f"got {len(examples)}."
            )

        features = [example["factors"] for example in examples]
        outcomes = [float(example["outcome"]) for example in examples]
        coefficients, intercept = fit_ridge(
            features, outcomes, alpha=float(data.get("alpha", DEFAULT_ALPHA))
        )

        self.trained_at = datetime.now(timezone.utc).isoformat()
        artifact = {
            "schema": ARTIFACT_SCHEMA,
            "version": MODEL_VERSION,
            "feature_names": FEATURE_NAMES,
            "coefficients": [round(value, 8) for value in coefficients],
            "intercept": round(intercept, 8),
            "alpha": float(data.get("alpha", DEFAULT_ALPHA)),
            "training_rows": len(examples),
            "training_source": Path(training_path).name,
            "training_data_kind": "synthetic-fixture",
            "trained_at": self.trained_at,
        }
        validate_artifact(artifact)
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(
            json.dumps(artifact, indent=2) + "\n", encoding="utf-8"
        )
        self._artifact = artifact
        self.load_error = None
        return self.metadata()

    def load(self) -> bool:
        """Load the artifact if present.

        Never raises: a corrupt artifact must not make every dashboard route
        return 500 with no way to reset. The failure is recorded in
        ``load_error``, surfaced in ``metadata()``, and cleared by retraining.
        """

        self.load_error = None
        if not self.artifact_path.exists():
            return False
        try:
            artifact = validate_artifact(
                json.loads(self.artifact_path.read_text(encoding="utf-8"))
            )
        except (ModelArtifactError, json.JSONDecodeError, OSError) as error:
            self._artifact = None
            self.load_error = str(error)
            return False
        self._artifact = artifact
        self.trained_at = artifact.get("trained_at")
        return True

    def predict(self, factors: ScoreFactors) -> float:
        if self._artifact is None:
            raise ModelArtifactError("Local model is not trained")
        return predict_from_artifact(self._artifact, factors)

    def metadata(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "schema": ARTIFACT_SCHEMA,
            "ready": self.ready,
            "trained_at": self.trained_at,
            "artifact": str(self.artifact_path),
            "features": FEATURE_NAMES,
            "training_rows": (self._artifact or {}).get("training_rows"),
            "training_data_kind": (self._artifact or {}).get("training_data_kind"),
            "load_error": self.load_error,
        }
