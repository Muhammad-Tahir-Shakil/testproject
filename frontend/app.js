import {
  annotateWithLocalModel,
  getLocalModel,
  localModelDisagrees,
  setupLocalAI,
} from "./local-ai.js";
import {
  apiRequest,
  currentSession,
  finishLogin,
  identityFromSession,
  isConfigured,
  signOut,
  startLogin,
} from "./aws-client.js";

const $ = (id) => document.getElementById(id);
const config = window.RETAILFIXIT_CONFIG;
const RUN_STORAGE_KEY = "retailfixit-current-run";
const POLL_ATTEMPTS = 40;
const POLL_INTERVAL_MS = 1000;

const textSkillAliases = {
  hvac: ["hvac", "air conditioning", "air-conditioning", "chiller", "cooling"],
  refrigeration: ["refrigeration", "compressor", "condenser", "chiller"],
  electrical: [
    "electrical",
    "electric",
    "wiring",
    "circuit",
    "power",
    "switchgear",
    "transfer switch",
  ],
  plumbing: ["plumbing", "pipe", "piping", "water leak", "riser", "drain"],
  elevator: ["elevator", "lift"],
  controls: ["controls", "controller", "interlock", "automation", "bms"],
  solar: ["solar", "photovoltaic", "pv array"],
  battery: ["battery", "bess", "energy storage"],
  generator: ["generator", "genset", "standby"],
  "fire safety": ["fire", "suppression", "smoke"],
  "water treatment": ["water treatment", "water quality"],
};

let model = getLocalModel();
let catalog = null;
let selectedScenario = null;
let currentJob = null;
let currentVendors = [];
let latestResponse = null;
let latestJobId = "";
let apiReady = false;
let currentTrace = null;
let latestOverride = null;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function notice(message, kind = "good") {
  $("notice").textContent = message;
  $("notice").className = `notice ${kind}`;
}

function inferredSkills(job) {
  const text = `${job.job_type} ${job.title} ${job.details}`.toLowerCase();
  const explicit = new Set(
    (job.required_skills || []).map((skill) => skill.toLowerCase())
  );
  return Object.entries(textSkillAliases)
    .filter(
      ([skill, aliases]) =>
        !explicit.has(skill) && aliases.some((alias) => text.includes(alias))
    )
    .map(([skill]) => skill);
}

function allJobSkills(job) {
  return [...new Set([...(job.required_skills || []), ...inferredSkills(job)])];
}

function formatDate(value) {
  if (!value) return "No run yet";
  return new Date(value).toLocaleString([], {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function renderAuth() {
  const signedIn = Boolean(currentSession());
  $("auth-panel").classList.toggle("hidden", signedIn);
  $("app-panel").classList.toggle("hidden", !signedIn);
  $("login-button").classList.toggle("hidden", signedIn);
  $("logout-button").classList.toggle("hidden", !signedIn);
  $("auth-status").className = `badge ${signedIn ? "good" : "neutral"}`;
  $("auth-status").textContent = signedIn ? "Signed in" : "Not signed in";
  $("identity").textContent = signedIn ? identityFromSession() : "Cognito user";
  $("local-ai-status").textContent = model ? "Ready" : "Not setup";
  $("local-ai-detail").textContent = model
    ? model.version
    : "Train the local model for a second opinion";
  $("setup-ai").disabled = !signedIn;
  renderReadiness();
  updateRunButtons();
}

function renderReadiness() {
  const signedIn = Boolean(currentSession());
  const modelReady = Boolean(model);
  const catalogReady = Boolean(catalog && currentJob);
  $("auth-step").className = `readiness-step ${signedIn ? "complete" : "active"}`;
  $("auth-step").textContent = signedIn ? "1. Signed in" : "1. Sign in required";
  $("model-step").className = `readiness-step ${modelReady ? "complete" : "active"}`;
  $("model-step").textContent = modelReady
    ? "2. Browser model ready"
    : "2. Browser model setup required";
  $("dispatch-step").className = `readiness-step ${
    modelReady && catalogReady ? "active" : ""
  }`;
  $("dispatch-step").textContent =
    modelReady && catalogReady ? "3. Ready to dispatch" : "3. Dispatch a request";
  $("model-card-status").className = `tag ${modelReady ? "ready-tag" : ""}`;
  $("model-card-status").textContent = modelReady ? "Ready" : "Setup required";
}

function updateRunButtons() {
  const disabled =
    !currentSession() || !apiReady || !model || !catalog || !currentJob;
  renderReadiness();
  $("run-sample-job").disabled = disabled;
  $("run-manual-job").disabled = disabled;
  $("dispatch-help").textContent = !currentSession()
    ? "Sign in to connect the protected dispatch workflow."
    : !model
    ? "Setup the browser model above to enable dispatch."
    : !apiReady
    ? "Waiting for the AWS API connection."
    : "Results are generated for the current request; editing it requires a new dispatch.";
}

function renderModelDetails() {
  $("model-details").textContent = model
    ? `${model.version} · ${model.trainingRows} synthetic rows · in-sample R² ${model.rSquared} · trained ${formatDate(model.trainedAt)}. Weights stay in this browser session and never change the AWS ranking.`
    : "No local model is active. Setup is required before dispatch.";
  $("local-runtime-detail").textContent = model
    ? "Model weights are trained and retained in this browser session."
    : "Setup the local model before dispatching a request.";
  renderReadiness();
}

function renderScenarioOptions() {
  $("job-selector").innerHTML = catalog.scenarios
    .map(
      (scenario) =>
        `<option value="${escapeHtml(scenario.id)}">${escapeHtml(
          scenario.label
        )}</option>`
    )
    .join("");
}

function renderJobEditor() {
  if (!currentJob) return;
  const values = {
    "job-customer": currentJob.customer_name,
    "job-site": currentJob.site_name,
    "job-asset": currentJob.asset_label,
    "job-type": currentJob.job_type,
    "job-title": currentJob.title,
    "job-details": currentJob.details,
    "job-skills": currentJob.required_skills.join(", "),
    "job-region": currentJob.region,
    "job-sla": currentJob.sla_hours,
    "job-risk": currentJob.risk_level,
    "job-top-k": selectedScenario?.top_k || 3,
  };
  Object.entries(values).forEach(([id, value]) => {
    $(id).value = value;
  });
  $("job-status").className = "editor-status";
  $("job-status").textContent = `Ready to dispatch ${currentJob.job_id}. Edit the request to change the recommendation output.`;
  renderJobPreview();
  renderVendors();
}

function renderJobPreview() {
  if (!currentJob) {
    $("job-data").textContent = "Choose a customer request to begin.";
    return;
  }
  const skills = allJobSkills(currentJob);
  $("job-data").innerHTML = `
    <div class="preview-kicker">${escapeHtml(currentJob.job_id)} · ${escapeHtml(
    currentJob.job_type
  )}</div>
    <strong>${escapeHtml(currentJob.customer_name)}</strong>
    <span>${escapeHtml(currentJob.site_name)}</span>
    <span>${escapeHtml(currentJob.asset_label)}</span>
    <h3>${escapeHtml(currentJob.title)}</h3>
    <p>${escapeHtml(currentJob.details)}</p>
    <div class="tag-row">
      <span class="tag">${escapeHtml(currentJob.region)} region</span>
      <span class="tag">${currentJob.sla_hours}h SLA</span>
      <span class="tag ${
        currentJob.risk_level === "high" ? "risk-high" : ""
      }">${escapeHtml(currentJob.risk_level)} risk</span>
    </div>
    <small>Matching skills: ${
      skills.map(escapeHtml).join(", ") || "General service"
    }</small>
  `;
}

function renderVendors() {
  $("override-vendor").innerHTML = currentVendors.length
    ? currentVendors
        .map(
          (vendor) =>
            `<option value="${escapeHtml(vendor.vendor_id)}">${escapeHtml(
              vendor.name
            )} · ${
              vendor.available_capacity ??
              vendor.capacity_total - vendor.capacity_used
            } open</option>`
        )
        .join("")
    : '<option value="">Run a request first</option>';
}

function renderReviewReasons(response) {
  if (!response?.review_reasons?.length) return "";
  return `
    <div class="review-reasons">
      <strong>Why a human must decide</strong>
      <ul>${response.review_reasons
        .map((reason) => `<li>${escapeHtml(reason)}</li>`)
        .join("")}</ul>
    </div>
  `;
}

function renderDecisionSummary(response, override = latestOverride) {
  if (!response) {
    $("decision-summary").innerHTML =
      '<div class="callout">No dispatch decision has been recorded.</div>';
    $("decision-history").innerHTML = "";
    $("override-effect").className = "override-effect hidden";
    $("override-heading").textContent = "Record final decision";
    $("override-submit").textContent = "Record decision";
    return;
  }
  const aiVendor =
    response.recommended_vendor_name ||
    response.recommendations?.[0]?.vendor_name ||
    "No eligible vendor";
  const aiVendorId =
    response.recommended_vendor_id ||
    response.recommendations?.[0]?.vendor_id ||
    "";
  const finalVendor = override
    ? override.final_vendor_name ||
      currentVendors.find((vendor) => vendor.vendor_id === override.vendor_id)
        ?.name ||
      override.vendor_id
    : aiVendor;
  const decisionLabel =
    override?.decision_type === "confirmed"
      ? "AI recommendation confirmed"
      : override?.decision_type === "overridden"
      ? "Human override recorded"
      : response.decision_state === "ai_recommended"
      ? "AI recommendation ready"
      : "Manual review required";
  const stateLabel = override
    ? decisionLabel
    : response.decision_state === "ai_recommended"
    ? "AI recommendation ready"
    : "Manual review required";
  const stateClass = override
    ? override.decision_type === "confirmed"
      ? "good"
      : "human"
    : response.decision_state === "ai_recommended"
    ? "good"
    : "review";
  const margin =
    response.decision_margin === null || response.decision_margin === undefined
      ? "—"
      : response.decision_margin.toFixed(2);
  $("decision-summary").innerHTML = `
    <div class="decision-summary-main">
      <div><span class="decision-label">FINAL DISPATCH DECISION</span><strong>${escapeHtml(
        finalVendor
      )}</strong><small>${
    override
      ? `Decision v${override.decision_version} · ${
          override.idempotent ? "existing decision returned" : "new revision recorded"
        }`
      : "Current AI recommendation"
  }</small></div>
      <span class="decision-state ${stateClass}">${stateLabel}</span>
    </div>
    <div class="decision-stats">
      <span><b>${escapeHtml(aiVendor)}</b><small>AI recommendation${
    aiVendorId ? ` · ${escapeHtml(aiVendorId)}` : ""
  }</small></span>
      <span><b>${
        response.recommendations?.[0]?.rank
          ? `#${response.recommendations[0].rank}`
          : "—"
      } of ${
    response.eligible_candidate_count ?? currentVendors.length
  }</b><small>eligible vendors</small></span>
      <span><b>${
        response.candidate_count ?? currentVendors.length
      }</b><small>vendor profiles received</small></span>
      <span><b>${
        response.recommendations?.[0]?.score ?? "—"
      }</b><small>top AWS score</small></span>
      <span><b>${margin}</b><small>margin over rank 2</small></span>
      <span><b>${
        response.recommendations?.[0]
          ? `${Math.round(response.recommendations[0].confidence * 100)}%`
          : "—"
      }</b><small>confidence</small></span>
    </div>
    ${renderReviewReasons(response)}
  `;
  if (override) {
    const original = override.previous_vendor_name || aiVendor;
    $("override-effect").className = "override-effect";
    $("override-effect").innerHTML = `
      <strong>${escapeHtml(decisionLabel)}</strong>
      <span>AI selected ${escapeHtml(original)}${
      override.previous_rank ? ` at rank #${override.previous_rank}` : ""
    }; the dispatcher selected ${escapeHtml(finalVendor)}.</span>
      <span>${escapeHtml(override.reason)} · ${escapeHtml(
      override.actor_id
    )} · ${formatDate(override.recorded_at)} · ${
      override.idempotent ? "idempotent repeat" : `revision ${override.decision_version}`
    }</span>
    `;
    $("override-heading").textContent = "Update decision";
    $("override-submit").textContent = "Update decision";
  } else {
    $("override-effect").className = "override-effect hidden";
    $("override-heading").textContent = "Record final decision";
    $("override-submit").textContent = "Record decision";
  }
  const history = override?.revision_history || [];
  $("decision-history").innerHTML = history.length
    ? `<strong>Decision history</strong>${history
        .map(
          (revision) =>
            `<div class="decision-history-item"><b>v${revision.decision_version} · ${
              revision.decision_type === "confirmed" ? "AI confirmed" : "Human override"
            }</b><span>${escapeHtml(revision.final_vendor_name || revision.final_vendor_id)} · ${escapeHtml(revision.actor_id)} · ${formatDate(revision.recorded_at)}</span></div>`
        )
        .join("")}`
    : "";
}

function renderRecommendations(response) {
  if (!response) {
    $("recommendations").innerHTML =
      '<div class="callout">Dispatch a request to see job-specific vendor evidence.</div>';
    $("recommendation-summary").className = "result-badge neutral";
    $("recommendation-summary").textContent = "No result yet";
    renderDecisionSummary(null);
    return;
  }

  // Order is the AWS response order and is never re-sorted locally. See the
  // note at the top of local-ai.js.
  const recommendations = annotateWithLocalModel(
    response.recommendations || [],
    model
  );
  const aiVendorId =
    response.recommended_vendor_id || recommendations[0]?.vendor_id;
  const finalVendorId = latestOverride?.vendor_id;
  const finalDecisionTag =
    latestOverride?.decision_type === "confirmed"
      ? "AI recommendation confirmed"
      : "Human override";

  if (!recommendations.length) {
    $("recommendations").innerHTML = `
      <div class="empty-result">
        <strong>No eligible vendor was found for this request.</strong>
        <span>Review the SLA, region, risk level, or required skills and dispatch again.</span>
      </div>
    `;
  } else {
    const disagreementBanner = localModelDisagrees(recommendations)
      ? `<div class="callout warn-callout">The browser model would order these candidates differently from the AWS ranking. The AWS ranking is the audited decision; treat the disagreement as a prompt to check the evidence below, not as a second recommendation.</div>`
      : "";

    $("recommendations").innerHTML =
      disagreementBanner +
      recommendations
        .map((rec) => {
          const vendor = currentVendors.find(
            (item) => item.vendor_id === rec.vendor_id
          );
          const vendorSkills = new Set(
            (vendor?.skills || []).map((skill) => skill.toLowerCase())
          );
          const matchedSkills = allJobSkills(currentJob).filter((skill) =>
            vendorSkills.has(skill.toLowerCase())
          );
          const decisionTags = [
            rec.vendor_id === aiVendorId
              ? '<span class="decision-tag ai">AI recommendation</span>'
              : "",
            rec.vendor_id === finalVendorId
              ? `<span class="decision-tag human">${finalDecisionTag}</span>`
              : "",
            rec.agrees === false
              ? `<span class="decision-tag review">Browser model ranks #${rec.local_rank}</span>`
              : "",
          ].join("");
          const factors = Object.entries(rec.score_factors)
            .map(
              ([name, value]) =>
                `<div class="factor"><span>${escapeHtml(
                  name.replaceAll("_", " ")
                )}</span><b>${Math.round(value * 100)}%</b></div>`
            )
            .join("");
          return `
        <article class="rec ${rec.abstained ? "needs-review" : ""} ${
            rec.vendor_id === finalVendorId ? "human-selected" : ""
          }">
          <div class="rec-top">
            <div><span class="rank">OPTION ${rec.rank}</span><strong>${escapeHtml(
            rec.vendor_name
          )}</strong><small>${escapeHtml(rec.vendor_id)}</small></div>
            <div class="score">${rec.score}<small>/ 100</small></div>
          </div>
          <div class="decision-tags">${decisionTags}</div>
          <div class="rec-status ${
            rec.abstained || response.status !== "recommendations_ready"
              ? "review"
              : "eligible"
          }">${
            rec.abstained
              ? "Low confidence · manual review"
              : response.status !== "recommendations_ready"
              ? "Eligible candidate · auto-dispatch blocked"
              : "Eligible for dispatch"
          }</div>
          <p>${escapeHtml(rec.rationale)}</p>
          <div class="evidence"><strong>Evidence for this request</strong><span>${
            matchedSkills.length
              ? `Matched ${matchedSkills.map(escapeHtml).join(", ")}`
              : "No direct skill match; score relies on operational fit"
          }</span><span>${
            vendor
              ? `${
                  vendor.available_capacity ??
                  vendor.capacity_total - vendor.capacity_used
                } open of ${vendor.capacity_total} capacity · ${
                  vendor.avg_response_hours
                }h average response · ${
                  vendor.sample_size
                    ? `${vendor.sample_size} completed jobs on record`
                    : "no completion history on record"
                }`
              : "Vendor profile unavailable"
          }</span></div>
          <div class="factors">${factors}</div>
          <div class="rec-footer">AWS decision ${rec.score} · browser second opinion ${
            rec.local_score ?? "not setup"
          } · confidence ${Math.round(
            rec.confidence * 100
          )}% (fit ${Math.round(
            rec.requirement_fit * 100
          )}%, evidence ${Math.round(rec.evidence_strength * 100)}%)</div>
        </article>
      `;
        })
        .join("");
  }

  $("recommendation-summary").className = `result-badge ${
    response.status === "recommendations_ready" ? "good" : "review"
  }`;
  $("recommendation-summary").textContent = latestOverride
    ? latestOverride.decision_type === "confirmed"
      ? `AI recommendation confirmed · v${latestOverride.decision_version}`
      : `Human override recorded · v${latestOverride.decision_version}`
    : response.status === "recommendations_ready"
    ? `AI selected #1 of ${
        response.eligible_candidate_count ?? recommendations.length
      }`
    : "Manual review required";
  renderDecisionSummary(response);
}

function markJobDirty() {
  collectJobInput();
  latestResponse = null;
  latestOverride = null;
  currentTrace = null;
  $("job-status").className = "editor-status dirty";
  $("job-status").textContent =
    "Unsaved request changes · dispatch again to refresh recommendations.";
  $("recommendation-summary").className = "result-badge review";
  $("recommendation-summary").textContent = "Results need rerun";
  renderJobPreview();
}

function collectJobInput() {
  currentJob = {
    ...currentJob,
    customer_name: $("job-customer").value.trim(),
    site_name: $("job-site").value.trim(),
    asset_label: $("job-asset").value.trim(),
    job_type: $("job-type").value.trim(),
    title: $("job-title").value.trim(),
    details: $("job-details").value.trim(),
    required_skills: $("job-skills")
      .value.split(",")
      .map((skill) => skill.trim())
      .filter(Boolean),
    region: $("job-region").value,
    sla_hours: Number($("job-sla").value),
    risk_level: $("job-risk").value,
  };
}

function selectScenario(scenarioId) {
  selectedScenario =
    catalog.scenarios.find((scenario) => scenario.id === scenarioId) ||
    catalog.scenarios[0];
  currentJob = clone(selectedScenario.job);
  currentVendors = clone(catalog.vendors);
  latestResponse = null;
  latestOverride = null;
  currentTrace = null;
  latestJobId = currentJob.job_id;
  $("last-run").textContent = "No run yet";
  $("last-run-detail").textContent = "Select a request and dispatch it";
  renderJobEditor();
  renderRecommendations(null);
}

function renderTrace(trace) {
  $("trace").innerHTML = trace.steps
    .map(
      (step) => `
    <div class="trace-step ${escapeHtml(step.status)}">
      <span class="dot"></span>
      <strong>${escapeHtml(step.name)}</strong>
      <small>${escapeHtml(step.status)}${
        step.detail ? ` · ${escapeHtml(step.detail)}` : ""
      }</small>
    </div>
  `
    )
    .join("");
}

function markBrowserResult(trace) {
  const browserStatus = trace.status === "completed" ? "completed" : "failed";
  return {
    ...trace,
    steps: trace.steps.map((step) =>
      step.name === "Browser result"
        ? {
            ...step,
            status: browserStatus,
            detail:
              browserStatus === "completed"
                ? "Result rendered"
                : "Result unavailable",
          }
        : step
    ),
  };
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForRun(requestId) {
  for (let attempt = 0; attempt < POLL_ATTEMPTS; attempt += 1) {
    const trace = await apiRequest(`/runs/${requestId}`);
    renderTrace(trace);
    if (trace.status === "completed" || trace.status === "failed") {
      const finalTrace = markBrowserResult(trace);
      renderTrace(finalTrace);
      return finalTrace;
    }
    await wait(POLL_INTERVAL_MS);
  }
  throw new Error(
    `The AWS workflow did not complete within ${
      (POLL_ATTEMPTS * POLL_INTERVAL_MS) / 1000
    } seconds. The run may still finish; reload to pick up the trace.`
  );
}

async function loadCatalog() {
  const response = await fetch("./jobs.json?rev=20260816-3");
  if (!response.ok)
    throw new Error("Customer request catalog could not be loaded.");
  catalog = await response.json();
  renderScenarioOptions();
  selectScenario(catalog.scenarios[0].id);
}

async function restoreRun() {
  const requestId = sessionStorage.getItem(RUN_STORAGE_KEY);
  if (!requestId || !catalog) return;
  try {
    const trace = await apiRequest(`/runs/${requestId}`);
    const scenario = catalog.scenarios.find(
      (item) => item.job.job_id === trace.job_id
    );
    if (!scenario || !trace.recommendation) return;
    selectScenario(scenario.id);
    currentTrace = trace;
    latestResponse = trace.recommendation;
    latestOverride = trace.decision || trace.override || null;
    latestJobId = trace.job_id;
    $("last-run").textContent = formatDate(latestResponse.generated_at);
    $("last-run-detail").textContent = `${latestJobId} · ${requestId.slice(0, 8)}`;
    $("job-status").className = latestOverride
      ? "editor-status complete"
      : "editor-status";
    $("job-status").textContent = latestOverride
      ? `Restored ${latestJobId} with a human override.`
      : `Restored the latest result for ${latestJobId}.`;
    renderRecommendations(latestResponse);
    renderTrace(currentTrace);
  } catch {
    sessionStorage.removeItem(RUN_STORAGE_KEY);
  }
}

async function setupModel() {
  $("setup-ai").disabled = true;
  try {
    model = await setupLocalAI(config.trainingUrl);
    renderAuth();
    renderModelDetails();
    notice(
      `Browser model ready (${model.trainingRows} synthetic rows, in-sample R² ${model.rSquared}). It provides a second opinion only and never changes the AWS ranking.`
    );
  } catch (error) {
    notice(error.message, "error");
  } finally {
    $("setup-ai").disabled = false;
    renderReadiness();
    updateRunButtons();
  }
}

async function dispatchCurrentJob() {
  if (!model) {
    notice(
      "Setup the browser model before dispatching the hybrid recommendation.",
      "error"
    );
    return;
  }
  collectJobInput();
  if (
    !currentJob.customer_name ||
    !currentJob.site_name ||
    !currentJob.asset_label ||
    !currentJob.job_type ||
    !currentJob.title ||
    !currentJob.details ||
    !currentJob.required_skills.length
  ) {
    notice(
      "Customer, site, asset, job type, title, details, and at least one skill are required.",
      "error"
    );
    return;
  }
  if (!Number.isFinite(currentJob.sla_hours) || currentJob.sla_hours <= 0) {
    notice("SLA hours must be a positive number.", "error");
    return;
  }
  const topK = Number($("job-top-k").value);
  if (!Number.isFinite(topK) || topK < 1 || topK > 5) {
    notice("Show results must be between 1 and 5.", "error");
    return;
  }

  $("run-sample-job").disabled = true;
  $("run-manual-job").disabled = true;
  $("job-status").className = "editor-status running";
  $("job-status").textContent =
    "Dispatching the current request through the protected AWS workflow…";
  $("security").textContent =
    "Cognito JWT verified. Request accepted by API Gateway; waiting for the AWS worker.";
  renderJobPreview();
  try {
    const accepted = await apiRequest("/runs", {
      method: "POST",
      body: JSON.stringify({
        job: currentJob,
        vendors: currentVendors,
        top_k: topK,
      }),
    });
    latestJobId = accepted.job_id;
    sessionStorage.setItem(RUN_STORAGE_KEY, accepted.request_id);
    const trace = await waitForRun(accepted.request_id);
    if (trace.status === "failed")
      throw new Error(trace.error || "AWS workflow failed.");
    currentTrace = trace;
    latestOverride = null;
    latestResponse = trace.recommendation;
    $("last-run").textContent = formatDate(latestResponse.generated_at);
    $("last-run-detail").textContent = `${latestJobId} · ${accepted.request_id.slice(
      0,
      8
    )}`;
    $("job-status").className = "editor-status complete";
    $("job-status").textContent = `Completed ${latestJobId}. Edit the request and dispatch again for a new result.`;
    $("api-status").textContent = "Connected";
    $("aws-runtime-detail").textContent = `AWS workflow completed · request ${accepted.request_id.slice(
      0,
      8
    )}`;
    renderRecommendations(latestResponse);
    notice(
      latestResponse.status === "recommendations_ready"
        ? `Dispatch completed. AWS recommends ${latestResponse.recommended_vendor_name}.`
        : `Dispatch completed, but a human must decide: ${latestResponse.review_reasons[0]}`,
      latestResponse.status === "recommendations_ready" ? "good" : "warn"
    );
  } catch (error) {
    $("job-status").className = "editor-status error";
    $("job-status").textContent =
      "Dispatch failed. Review the request and try again.";
    notice(error.message, "error");
  } finally {
    updateRunButtons();
  }
}

async function runSampleJob() {
  if (selectedScenario) selectScenario(selectedScenario.id);
  await dispatchCurrentJob();
}

async function runManualJob() {
  await dispatchCurrentJob();
}

async function submitOverride(event) {
  event.preventDefault();
  if (!latestJobId || !latestResponse) {
    notice("Dispatch a request before recording an override.", "error");
    return;
  }
  if (!$("override-vendor").value) {
    notice("Select a vendor to assign.", "error");
    return;
  }
  if ($("override-reason").value.trim().length < 3) {
    notice("Give a reason of at least three characters.", "error");
    return;
  }
  $("override-submit").disabled = true;
  notice("Recording the final decision in AWS…");
  try {
    const response = await apiRequest("/overrides", {
      method: "POST",
      body: JSON.stringify({
        job_id: latestJobId,
        vendor_id: $("override-vendor").value,
        reason: $("override-reason").value.trim(),
        // Replaced server-side with the verified Cognito subject; sent only
        // to satisfy the request contract.
        actor_id: "cognito-session",
        request_id: currentTrace?.request_id,
      }),
    });
    $("override-reason").value = "";
    latestOverride = response;
    if (currentTrace) {
      currentTrace = {
        ...currentTrace,
        decision_state:
          response.decision_type === "confirmed"
            ? "ai_recommendation_confirmed"
            : "human_overridden",
        final_vendor_id: response.vendor_id,
        final_vendor_name: response.final_vendor_name,
        override: response,
        decision: response,
        steps: currentTrace.steps.map((step) =>
          step.name === "Human decision"
            ? {
                ...step,
                status: "completed",
                detail: `${response.decision_type} v${response.decision_version}: ${
                  response.final_vendor_name || response.vendor_id
                }`,
              }
            : step
        ),
      };
      renderTrace(currentTrace);
    }
    renderRecommendations(latestResponse);
    notice(
      response.idempotent
        ? `Decision v${response.decision_version} already exists for this vendor; no new revision was created.`
        : `${response.decision_type === "confirmed" ? "AI recommendation confirmed" : "Human override recorded"} as decision v${response.decision_version}.`
    );
  } catch (error) {
    notice(error.message, "error");
  } finally {
    $("override-submit").disabled = false;
  }
}

function bindJobInputs() {
  [
    "job-customer",
    "job-site",
    "job-asset",
    "job-type",
    "job-title",
    "job-details",
    "job-skills",
    "job-region",
    "job-sla",
    "job-risk",
    "job-top-k",
  ].forEach((id) => $(id).addEventListener("input", markJobDirty));
  ["job-region", "job-risk", "job-top-k"].forEach((id) =>
    $(id).addEventListener("change", markJobDirty)
  );
  $("job-selector").addEventListener("change", (event) =>
    selectScenario(event.target.value)
  );
}

async function start() {
  if (!isConfigured()) {
    notice(
      "GitHub Pages configuration is missing. Deploy through GitHub Actions first.",
      "error"
    );
    renderAuth();
    return;
  }
  try {
    await finishLogin();
  } catch (error) {
    notice(error.message, "error");
  }
  renderAuth();
  renderModelDetails();
  try {
    await loadCatalog();
  } catch (error) {
    notice(error.message, "error");
  }
  if (currentSession()) {
    try {
      await apiRequest("/health");
      apiReady = true;
      $("api-status").textContent = "Connected";
      $("security").textContent =
        "Cognito JWT verified. AWS API is ready for the active request.";
      updateRunButtons();
      await restoreRun();
    } catch (error) {
      $("api-status").textContent = "Unavailable";
      notice(error.message, "error");
    }
  }
}

$("login-button").addEventListener("click", () =>
  startLogin().catch((error) => notice(error.message, "error"))
);
$("auth-action").addEventListener("click", () =>
  startLogin().catch((error) => notice(error.message, "error"))
);
$("logout-button").addEventListener("click", signOut);
$("setup-ai").addEventListener("click", setupModel);
$("run-sample-job").addEventListener("click", runSampleJob);
$("run-manual-job").addEventListener("click", runManualJob);
$("override-form").addEventListener("submit", submitOverride);
bindJobInputs();
start();
