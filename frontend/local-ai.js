/**
 * Browser-local second-opinion model.
 *
 * Deliberate design points:
 *
 * - This model NEVER changes the dispatch ranking. The AWS response is the
 *   audited decision; the browser score is displayed alongside it as an
 *   independent read. Re-sorting the cards by a score that exists only in one
 *   operator's browser session would mean the UI showed a different ordering
 *   from the one written to the S3 audit trail, which is precisely the kind of
 *   silent divergence an auditor should be able to rule out.
 * - Weights live in sessionStorage and die with the tab. Nothing is uploaded.
 * - Training data is the same synthetic fixture the server model uses, so the
 *   two are comparable rather than arbitrary.
 */

const STORAGE_KEY = "retailfixit-local-ai-v2";
const MODEL_VERSION = "hybrid-browser-v2";

export const featureNames = [
  "availability",
  "capacity",
  "skill_match",
  "region_match",
  "completion_rate",
  "similar_job_rate",
  "rework_history",
  "sla_fit",
  "risk_fit",
];

const LEARNING_RATE = 0.08;
const ITERATIONS = 3000;

function trainLinearModel(data) {
  const rows = data.examples;
  if (!Array.isArray(rows) || rows.length < featureNames.length) {
    throw new Error(
      `Training fixture needs at least ${featureNames.length} examples.`
    );
  }
  const weights = Array(featureNames.length).fill(0);
  let bias = rows.reduce((sum, row) => sum + row.outcome, 0) / rows.length;

  // Small batch gradient descent: transparent, local, and dependency-free.
  for (let iteration = 0; iteration < ITERATIONS; iteration += 1) {
    const gradients = Array(featureNames.length).fill(0);
    let biasGradient = 0;
    rows.forEach((row) => {
      const prediction = row.factors.reduce(
        (sum, value, index) => sum + value * weights[index],
        bias
      );
      const error = prediction - row.outcome;
      row.factors.forEach((value, index) => {
        gradients[index] += error * value;
      });
      biasGradient += error;
    });
    weights.forEach((_, index) => {
      weights[index] -= (LEARNING_RATE * gradients[index]) / rows.length;
    });
    bias -= (LEARNING_RATE * biasGradient) / rows.length;
  }

  // In-sample fit, reported in the UI so the operator can see that this is a
  // small model on synthetic data rather than a validated production model.
  const mean = rows.reduce((sum, row) => sum + row.outcome, 0) / rows.length;
  let ssRes = 0;
  let ssTot = 0;
  rows.forEach((row) => {
    const prediction = row.factors.reduce(
      (sum, value, index) => sum + value * weights[index],
      bias
    );
    ssRes += (row.outcome - prediction) ** 2;
    ssTot += (row.outcome - mean) ** 2;
  });

  return {
    version: MODEL_VERSION,
    featureNames,
    weights,
    bias,
    trainingRows: rows.length,
    rSquared: ssTot === 0 ? 0 : Number((1 - ssRes / ssTot).toFixed(4)),
    trainingDataKind: "synthetic-fixture",
    trainedAt: new Date().toISOString(),
  };
}

export async function setupLocalAI(trainingUrl) {
  const response = await fetch(trainingUrl);
  if (!response.ok) throw new Error("Local training fixture could not be loaded.");
  const payload = await response.json();
  if (JSON.stringify(payload.feature_names) !== JSON.stringify(featureNames)) {
    throw new Error("Training fixture features do not match ScoreFactors.");
  }
  const model = trainLinearModel(payload);
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(model));
  return model;
}

export function getLocalModel() {
  try {
    const model = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "null");
    if (!model || model.version !== MODEL_VERSION) return null;
    if (JSON.stringify(model.featureNames) !== JSON.stringify(featureNames)) {
      return null;
    }
    return model;
  } catch {
    return null;
  }
}

export function predictLocalScore(model, factors) {
  if (!model) return null;
  const values = featureNames.map((name) => Number(factors[name] || 0));
  const prediction = values.reduce(
    (sum, value, index) => sum + value * model.weights[index],
    model.bias
  );
  return Math.max(0, Math.min(100, prediction * 100));
}

/**
 * Attach the browser model's opinion without touching rank or score.
 *
 * `local_score` is the second opinion. `agrees` records whether the browser
 * model would have put this vendor in the same position as AWS did, which is
 * what the UI surfaces when the two disagree.
 */
export function annotateWithLocalModel(recommendations, model) {
  const scored = recommendations.map((rec) => ({
    ...rec,
    local_score: (() => {
      const value = predictLocalScore(model, rec.score_factors);
      return value === null ? null : Number(value.toFixed(2));
    })(),
  }));

  if (!model || scored.some((rec) => rec.local_score === null)) {
    return scored.map((rec) => ({ ...rec, local_rank: null, agrees: null }));
  }

  const localOrder = [...scored]
    .sort((left, right) => right.local_score - left.local_score)
    .map((rec) => rec.vendor_id);

  return scored.map((rec) => {
    const localRank = localOrder.indexOf(rec.vendor_id) + 1;
    return { ...rec, local_rank: localRank, agrees: localRank === rec.rank };
  });
}

export function localModelDisagrees(annotated) {
  return annotated.some((rec) => rec.agrees === false);
}
