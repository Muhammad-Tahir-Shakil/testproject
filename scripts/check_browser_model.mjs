/**
 * Execute the browser second-opinion model outside a browser.
 *
 * frontend/local-ai.js needs nothing from the DOM beyond sessionStorage and
 * fetch, so two small stubs are enough to run the real module against the real
 * fixture. Checks what would otherwise only surface as numbers looking wrong on
 * the dashboard: convergence, recovery of the declared ground truth, prediction
 * bounds, that annotation never reorders the AWS ranking, and that a
 * feature-list mismatch is rejected rather than silently mis-scoring.
 *
 * Run:  node scripts/check_browser_model.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const fixturePath = join(root, "frontend", "training.json");
const fixture = JSON.parse(readFileSync(fixturePath, "utf8"));

// --- minimal browser shims -------------------------------------------------
const store = new Map();
globalThis.sessionStorage = {
  getItem: (key) => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => store.set(key, String(value)),
  removeItem: (key) => store.delete(key),
};
globalThis.fetch = async (url) => ({
  ok: true,
  json: async () => {
    if (String(url).endsWith("training.json")) return fixture;
    throw new Error(`Unexpected fetch: ${url}`);
  },
});

const {
  annotateWithLocalModel,
  featureNames,
  getLocalModel,
  localModelDisagrees,
  predictLocalScore,
  setupLocalAI,
} = await import(join(root, "frontend", "local-ai.js"));

const failures = [];
let checks = 0;

function check(condition, message) {
  checks += 1;
  if (!condition) failures.push(message);
}

// --- training --------------------------------------------------------------
const model = await setupLocalAI("./training.json");

check(
  model.weights.every(Number.isFinite) && Number.isFinite(model.bias),
  "Gradient descent produced a non-finite weight or bias (diverged)."
);
check(
  model.featureNames.join(",") === featureNames.join(","),
  "Trained model feature order does not match the exported featureNames."
);
check(
  model.trainingRows === fixture.examples.length,
  `Model reports ${model.trainingRows} rows; fixture has ${fixture.examples.length}.`
);
check(
  model.rSquared > 0.7,
  `In-sample R² is ${model.rSquared}; expected the fit to explain most variance.`
);

const truth = fixture.ground_truth_weights;
console.log(`browser model: ${model.version}`);
console.log(`rows=${model.trainingRows}  in-sample R²=${model.rSquared}\n`);
console.log(`${"feature".padEnd(20)}${"fitted".padStart(9)}${"truth".padStart(9)}`);
featureNames.forEach((name, index) => {
  console.log(
    `${name.padEnd(20)}${model.weights[index].toFixed(4).padStart(9)}${truth[name]
      .toFixed(4)
      .padStart(9)}`
  );
});
console.log(`${"(bias)".padEnd(20)}${model.bias.toFixed(4).padStart(9)}\n`);

// `availability` is constant in the fixture (the eligibility gate filters
// unavailable vendors before scoring), so its weight carries no signal and is
// excluded from the recovery check.
featureNames.forEach((name, index) => {
  if (name === "availability") return;
  check(
    Math.abs(model.weights[index] - truth[name]) < 0.12,
    `Weight for ${name} is ${model.weights[index].toFixed(4)}, ` +
      `too far from the declared ground truth ${truth[name]}.`
  );
});

// --- persistence -----------------------------------------------------------
const restored = getLocalModel();
check(restored !== null, "Model did not round-trip through sessionStorage.");
check(
  restored && restored.weights.join(",") === model.weights.join(","),
  "Restored weights differ from the trained weights."
);

// --- prediction bounds -----------------------------------------------------
const extremes = [0, 1];
for (const value of extremes) {
  const factors = Object.fromEntries(featureNames.map((name) => [name, value]));
  const score = predictLocalScore(model, factors);
  check(
    score !== null && score >= 0 && score <= 100,
    `Prediction ${score} for all-${value} factors is outside 0-100.`
  );
}
check(
  predictLocalScore(null, {}) === null,
  "predictLocalScore should return null when no model is trained."
);

// --- ranking must not move -------------------------------------------------
function recommendation(vendorId, rank, skillMatch) {
  return {
    vendor_id: vendorId,
    rank,
    score: 90 - rank,
    score_factors: Object.fromEntries(
      featureNames.map((name) => [name, name === "skill_match" ? skillMatch : 0.9])
    ),
  };
}

// Deliberately inverted: the vendor the browser model likes least is ranked #1.
const ranked = [
  recommendation("V-1", 1, 0.1),
  recommendation("V-2", 2, 1.0),
  recommendation("V-3", 3, 0.5),
];
const annotated = annotateWithLocalModel(ranked, model);

check(
  annotated.map((item) => item.vendor_id).join(",") === "V-1,V-2,V-3",
  "annotateWithLocalModel reordered the AWS ranking; it must never do that."
);
check(
  annotated.every((item) => typeof item.local_score === "number"),
  "Annotated recommendations are missing a local_score."
);
check(
  annotated.every((item) => item.local_rank >= 1 && item.local_rank <= 3),
  "local_rank is outside the candidate range."
);
check(
  localModelDisagrees(annotated) === true,
  "The browser model should flag disagreement on a deliberately inverted ranking."
);

const agreeing = annotateWithLocalModel(
  [recommendation("V-1", 1, 1.0), recommendation("V-2", 2, 0.2)],
  model
);
check(
  localModelDisagrees(agreeing) === false,
  "The browser model should not flag disagreement when the orders match."
);

// --- no model -> no opinion, and no crash ----------------------------------
const withoutModel = annotateWithLocalModel(ranked, null);
check(
  withoutModel.every(
    (item) => item.local_score === null && item.agrees === null
  ),
  "Without a trained model the annotation should be null, not a guess."
);

// --- feature drift is rejected ---------------------------------------------
globalThis.fetch = async () => ({
  ok: true,
  json: async () => ({ feature_names: ["a", "b"], examples: [] }),
});
let rejected = false;
try {
  await setupLocalAI("./training.json");
} catch {
  rejected = true;
}
check(rejected, "A fixture with mismatched features should be rejected.");

// ---------------------------------------------------------------------------
console.log("-".repeat(62));
if (failures.length) {
  console.error(`FAILED: ${failures.length} problem(s) across ${checks} checks\n`);
  for (const failure of failures) console.error(`  - ${failure}`);
  process.exit(1);
}
console.log(`PASSED: ${checks} checks`);
