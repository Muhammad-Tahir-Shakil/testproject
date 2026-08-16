const $ = (id) => document.getElementById(id);

let dashboardState = null;
let activeJobId = "JOB-1001";
// Set while the operator is editing the manual form. Refresh must not
// overwrite in-progress typing, which is what the previous unconditional
// prefill did on every poll and on every Refresh click.
let manualFormDirty = false;

const DISPATCHER_ID_KEY = "dispatcher-id";

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function dispatcherId() {
  const stored = sessionStorage.getItem(DISPATCHER_ID_KEY);
  if (stored) return stored;
  const generated = `dispatcher-local-${Math.random().toString(36).slice(2, 8)}`;
  sessionStorage.setItem(DISPATCHER_ID_KEY, generated);
  return generated;
}

function showNotice(message, kind = "good") {
  const notice = $("notice");
  notice.textContent = message;
  notice.className = `notice ${kind}`;
}

function clearNotice() {
  $("notice").className = "notice hidden";
}

/**
 * FastAPI returns `detail` as a string for HTTPException but as a list of
 * objects for request-validation errors. Rendering the list directly produced
 * "[object Object]", which is what an operator saw for every 422.
 */
function formatDetail(detail, status) {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const messages = detail.map((item) => {
      const field = Array.isArray(item.loc)
        ? item.loc.filter((part) => part !== "body").join(".")
        : "";
      return field ? `${field}: ${item.msg}` : item.msg;
    });
    return messages.join(" · ") || `Request failed with ${status}`;
  }
  return `Request failed with ${status}`;
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "content-type": "application/json",
      "x-dispatcher-id": dispatcherId(),
      ...(options.headers || {}),
    },
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    throw new Error(formatDetail(data?.detail, response.status));
  }
  return data;
}

function activeDecisionFor(state, jobId) {
  return state.decisions?.find((item) => item.job_id === jobId)?.active || null;
}

function renderReviewReasons(latest) {
  if (!latest?.review_reasons?.length) return "";
  return `
    <div class="review-reasons">
      <strong>Human decision required</strong>
      <ul>${latest.review_reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>
    </div>
  `;
}

function renderRecommendations(state) {
  const latest = state.recommendations?.[0];
  if (!latest) {
    $("recommendations").innerHTML = "";
    return;
  }
  const decision = activeDecisionFor(state, activeJobId);
  const finalVendorId = decision?.final_vendor_id;
  $("recommendations").innerHTML = latest.recommendations
    .map((rec) => {
      const factors = Object.entries(rec.score_factors)
        .map(
          ([name, value]) =>
            `<div class="factor"><span>${escapeHtml(
              name.replaceAll("_", " ")
            )}</span><b>${Math.round(value * 100)}%</b></div>`
        )
        .join("");
      const modelLine =
        rec.model_score === null || rec.model_score === undefined
          ? `<span class="tag">rules only · ${rec.rule_score}</span>`
          : `<span class="tag">rules ${rec.rule_score} · model ${rec.model_score}</span>`;
      return `
      <article class="recommendation ${
        rec.vendor_id === finalVendorId ? "human-selected" : ""
      }">
        <div class="rec-top">
          <div><div class="rank">RANK #${rec.rank}</div><strong>${escapeHtml(
        rec.vendor_name
      )}</strong></div>
          <div><div class="score">${rec.score}</div><div class="score-label">score</div></div>
        </div>
        <div class="decision-tags">${
          rec.vendor_id === latest.recommended_vendor_id
            ? '<span class="tag">AI recommendation</span>'
            : ""
        }${
          rec.vendor_id === finalVendorId
          ? `<span class="tag human-tag">${
              decision?.decision_type === "confirmed"
                ? "AI recommendation confirmed"
                : "Human override"
            }</span>`
          : ""
      }</div>
        <p class="rationale">${escapeHtml(rec.rationale)}</p>
        <div class="factor-grid">${factors}</div>
        <div class="job-meta">
          <span class="tag">${Math.round(rec.confidence * 100)}% confidence</span>
          <span class="tag">fit ${Math.round(rec.requirement_fit * 100)}%</span>
          <span class="tag">evidence ${Math.round(rec.evidence_strength * 100)}%</span>
          ${modelLine}
          <span class="tag">${
            rec.abstained
              ? "Low confidence · manual review"
              : latest.status !== "recommendations_ready"
              ? "Eligible candidate · auto-dispatch blocked"
              : "Eligible for dispatch"
          }</span>
        </div>
      </article>
    `;
    })
    .join("");
}

function renderDecisionSummary(state) {
  const latest = state.recommendations?.[0];
  const decision = activeDecisionFor(state, activeJobId);
  if (!latest) {
    $("decision-summary").innerHTML =
      '<div class="empty-state">Run a job to see the decision summary.</div>';
    $("override-effect").className = "override-effect hidden";
    return;
  }
  const aiVendor =
    latest.recommended_vendor_name ||
    latest.recommendations?.[0]?.vendor_name ||
    "No eligible vendor";
  const finalVendor = decision?.final_vendor_name || decision?.final_vendor_id || aiVendor;
  const margin =
    latest.decision_margin === null || latest.decision_margin === undefined
      ? "—"
      : latest.decision_margin.toFixed(2);
  $("decision-summary").innerHTML = `
    <div class="decision-summary-main">
      <div><span class="decision-label">FINAL DISPATCH DECISION</span><strong>${escapeHtml(
        finalVendor
      )}</strong><small>${
    decision ? `Decision v${decision.decision_version} · selected by ${escapeHtml(decision.actor_id)}` : "Current AI recommendation"
  }</small></div>
      <span class="badge ${
        decision
          ? decision.decision_type === "confirmed" ? "good" : "human-badge"
          : latest.decision_state === "ai_recommended"
          ? "good"
          : "warn"
      }">${
    decision
      ? decision.decision_type === "confirmed" ? "AI recommendation confirmed" : "Human override recorded"
      : (latest.decision_state || "manual_review_required").replaceAll("_", " ")
  }</span>
    </div>
    <div class="decision-stats">
      <span><b>${escapeHtml(aiVendor)}</b><small>AI recommendation</small></span>
      <span><b>${latest.recommendations?.[0]?.rank || "—"} of ${
    latest.eligible_candidate_count ?? state.vendors?.length ?? 0
  }</b><small>eligible vendors</small></span>
      <span><b>${
        latest.candidate_count ?? state.vendors?.length ?? 0
      }</b><small>profiles received</small></span>
      <span><b>${latest.recommendations?.[0]?.score ?? "—"}</b><small>top score</small></span>
      <span><b>${margin}</b><small>margin over rank 2</small></span>
    </div>
    ${renderReviewReasons(latest)}
  `;
  if (decision) {
    $("override-effect").className = "override-effect";
    $("override-effect").innerHTML = `<strong>${
      decision.decision_type === "confirmed" ? "AI recommendation confirmed" : "Human override recorded"
    }</strong><span>AI selected ${escapeHtml(
      decision.ai_vendor_name || aiVendor
    )}; dispatcher selected ${escapeHtml(finalVendor)}.</span><span>${escapeHtml(
      decision.reason
    )} · ${escapeHtml(decision.actor_id)} · ${escapeHtml(decision.recorded_at)} · revision ${decision.decision_version}</span>`;
    $("override-heading").textContent = "Update decision";
    $("override-submit").textContent = "Update decision";
  } else {
    $("override-effect").className = "override-effect hidden";
    $("override-heading").textContent = "Record final decision";
    $("override-submit").textContent = "Record decision";
  }
  $("decision-history").innerHTML = decision?.job_id
    ? `<strong>Decision history</strong>${(state.decisions?.find((item) => item.job_id === activeJobId)?.revisions || [])
        .map((revision) => `<div class="decision-history-item"><b>v${revision.decision_version} · ${revision.decision_type === "confirmed" ? "AI confirmed" : "Human override"}</b><span>${escapeHtml(revision.final_vendor_name || revision.final_vendor_id)} · ${escapeHtml(revision.actor_id)} · ${escapeHtml(revision.recorded_at)}</span></div>`)
        .join("")}`
    : "";
}

function renderManualForm(job) {
  if (!job || manualFormDirty) return;
  const values = {
    "manual-customer": job.customer_name,
    "manual-site": job.site_name,
    "manual-asset": job.asset_label,
    "manual-type": job.job_type,
    "manual-region": job.region,
    "manual-sla": job.sla_hours,
    "manual-risk": job.risk_level,
    "manual-title": job.title,
    "manual-details": job.details,
    "manual-skills": (job.required_skills || []).join(", "),
  };
  Object.entries(values).forEach(([id, value]) => {
    $(id).value = value ?? "";
  });
}

function renderModelCard(model) {
  if (model?.load_error) {
    $("model-version").textContent = "Artifact rejected";
    $("model-detail").textContent = model.load_error;
    return;
  }
  $("model-version").textContent = model?.ready ? model.version : "Not trained";
  $("model-detail").textContent = model?.trained_at
    ? `${model.training_rows ?? "?"} synthetic rows · trained ${model.trained_at}`
    : "Offline model artifact";
}

function renderState(state) {
  dashboardState = state;
  const ready = state.setup_ready;
  $("system-status").textContent = ready ? "Ready" : "Not setup";
  $("system-detail").textContent = ready
    ? "Local workflow is connected."
    : "Click Setup to initialize local state.";
  renderModelCard(state.model);
  $("vendor-count").textContent = state.counts?.vendors ?? 0;
  $("audit-count").textContent = state.counts?.audit ?? 0;
  $("run-sample-button").disabled = !ready;
  $("run-manual-button").disabled = !ready;
  $("mode-badge").className = `badge ${ready ? "good" : "neutral"}`;
  $("mode-badge").textContent = ready ? "Local ready" : "Local setup required";

  const job = state.jobs?.[0];
  activeJobId = job?.job_id || activeJobId;
  const jobView = $("job-view");
  if (job) {
    jobView.className = "job-view";
    jobView.innerHTML = `<div class="job-card"><strong>${escapeHtml(
      job.job_id
    )} · ${escapeHtml(
      job.job_type
    )}</strong><div class="job-meta"><span class="tag">Region: ${escapeHtml(
      job.region
    )}</span><span class="tag">SLA: ${job.sla_hours}h</span><span class="tag">Risk: ${escapeHtml(
      job.risk_level
    )}</span><span class="tag">Skills: ${
      (job.required_skills || []).map(escapeHtml).join(", ") || "None"
    }</span></div></div>`;
  } else {
    jobView.className = "empty-state";
    jobView.textContent = "Setup the environment to load the sample job.";
  }
  renderManualForm(job);

  $("override-vendor").innerHTML = state.vendors?.length
    ? state.vendors
        .map(
          (vendor) =>
            `<option value="${escapeHtml(vendor.vendor_id)}">${escapeHtml(
              vendor.name
            )} (${escapeHtml(vendor.vendor_id)})</option>`
        )
        .join("")
    : '<option value="">Setup data first</option>';

  $("vendor-list").innerHTML = state.vendors?.length
    ? state.vendors
        .map(
          (vendor) =>
            `<div class="data-item"><strong>${escapeHtml(
              vendor.name
            )}</strong><span>${escapeHtml(vendor.vendor_id)} · ${
              vendor.active ? "active" : "inactive"
            } · ${vendor.capacity_total - vendor.capacity_used} free · ${
              vendor.sample_size ? `${vendor.sample_size} jobs` : "no history"
            }</span></div>`
        )
        .join("")
    : '<div class="empty-state">No vendor profiles loaded.</div>';

  $("event-list").innerHTML = state.events?.length
    ? state.events
        .map(
          (event) =>
            `<div class="timeline-item"><span class="timeline-dot"></span><strong>${escapeHtml(
              event.event_type
            )}</strong><small>${escapeHtml(event.created_at)}</small></div>`
        )
        .join("")
    : '<div class="empty-state">No local events yet.</div>';

  const auditShown = state.audit?.length ?? 0;
  const auditTotal = state.counts?.audit ?? 0;
  $("audit-list").innerHTML = state.audit?.length
    ? `<p class="list-caption">Showing the ${auditShown} most recent of ${auditTotal} records. Customer identifiers and free text are redacted, exactly as in the AWS S3 audit trail.</p>` +
      state.audit
        .map(
          (item) =>
            `<div class="audit-item"><strong>${escapeHtml(
              item.action
            )}</strong> · ${escapeHtml(item.created_at)}\n${escapeHtml(
              JSON.stringify(item.payload, null, 2)
            )}</div>`
        )
        .join("")
    : '<div class="empty-state">No audit records yet.</div>';

  renderRecommendations(state);
  renderDecisionSummary(state);
}

function renderInfrastructure(infrastructure) {
  const sections = [
    ["Local runtime", infrastructure.local],
    ["AWS mapping", infrastructure.aws_mapping],
    ["Security controls", infrastructure.security],
  ];
  $("infrastructure").innerHTML = sections
    .map(
      ([title, values]) => `
    <div class="infra-item"><strong>${escapeHtml(title)}</strong><span>${values
        .map(escapeHtml)
        .join(" · ")}</span></div>
  `
    )
    .join("");
}

/**
 * `preserveNotice` exists because every success path used to call
 * showNotice() and then refresh(), and refresh() opened with clearNotice() --
 * so no confirmation was ever visible.
 */
async function refresh({ preserveNotice = false } = {}) {
  if (!preserveNotice) clearNotice();
  try {
    const [state, infrastructure] = await Promise.all([
      request("/api/dashboard"),
      request("/api/infrastructure"),
    ]);
    renderState(state);
    renderInfrastructure(infrastructure);
  } catch (error) {
    showNotice(error.message, "error");
  }
}

function setRunButtons(enabled) {
  $("run-sample-button").disabled = !enabled;
  $("run-manual-button").disabled = !enabled;
}

async function setup() {
  $("setup-button").disabled = true;
  try {
    const result = await request("/api/setup", { method: "POST", body: "{}" });
    await refresh();
    showNotice(
      `Local setup complete: ${result.vendor_count} vendors and ${result.model.version} trained on ${result.model.training_rows} synthetic rows.`
    );
  } catch (error) {
    showNotice(error.message, "error");
  } finally {
    $("setup-button").disabled = false;
  }
}

function describeOutcome(result) {
  if (result.status === "recommendations_ready") {
    return `${result.job_id}: ${result.recommended_vendor_name} recommended.`;
  }
  const first = result.review_reasons?.[0] || "Manual review required.";
  return `${result.job_id}: manual review required — ${first}`;
}

async function runSample() {
  setRunButtons(false);
  try {
    const result = await request("/api/local/run-sample", {
      method: "POST",
      body: "{}",
    });
    manualFormDirty = false;
    await refresh();
    showNotice(
      describeOutcome(result),
      result.status === "recommendations_ready" ? "good" : "warn"
    );
  } catch (error) {
    showNotice(error.message, "error");
  } finally {
    setRunButtons(Boolean(dashboardState?.setup_ready));
  }
}

function collectManualJob() {
  return {
    job_id: `JOB-MANUAL-${Date.now()}`,
    customer_name: $("manual-customer").value.trim(),
    site_name: $("manual-site").value.trim(),
    asset_label: $("manual-asset").value.trim(),
    job_type: $("manual-type").value.trim(),
    title: $("manual-title").value.trim(),
    details: $("manual-details").value.trim(),
    required_skills: $("manual-skills")
      .value.split(",")
      .map((skill) => skill.trim())
      .filter(Boolean),
    region: $("manual-region").value,
    sla_hours: Number($("manual-sla").value),
    risk_level: $("manual-risk").value,
  };
}

async function runManual(event) {
  if (event) event.preventDefault();
  setRunButtons(false);
  const job = collectManualJob();
  if (!job.required_skills.length) {
    // The only rule the browser cannot express with a `required` attribute:
    // the field is a comma list and "  ,  " is non-empty but yields no skills.
    showNotice("List at least one required skill.", "error");
    setRunButtons(Boolean(dashboardState?.setup_ready));
    return;
  }
  try {
    const result = await request("/api/local/job-created", {
      method: "POST",
      body: JSON.stringify({
        job,
        vendors: dashboardState.vendors,
        top_k: Number($("manual-top-k").value),
      }),
    });
    manualFormDirty = false;
    await refresh();
    showNotice(
      describeOutcome(result),
      result.status === "recommendations_ready" ? "good" : "warn"
    );
  } catch (error) {
    showNotice(error.message, "error");
  } finally {
    setRunButtons(Boolean(dashboardState?.setup_ready));
  }
}

async function submitOverride(event) {
  event.preventDefault();
  $("override-submit").disabled = true;
  showNotice("Recording the final decision locally…");
  try {
    const result = await request("/api/local/override", {
      method: "POST",
      headers: {
        "X-Dispatcher-Id": dispatcherId(),
      },
      body: JSON.stringify({
        job_id: activeJobId,
        vendor_id: $("override-vendor").value,
        reason: $("override-reason").value,
        // Attribution comes from the X-Dispatcher-Id header; this is echoed
        // for the contract only and is replaced server-side.
        actor_id: dispatcherId(),
        request_id: dashboardState?.recommendations?.[0]?.request_id,
      }),
    });
    $("override-reason").value = "";
    await refresh();
    showNotice(
      result.idempotent
        ? `Decision v${result.decision_version} already exists; no new revision was created.`
        : `${result.decision_type === "confirmed" ? "AI recommendation confirmed" : "Human override recorded"} as decision v${result.decision_version}.`
    );
  } catch (error) {
    showNotice(error.message, "error");
  } finally {
    $("override-submit").disabled = false;
  }
}

function bindManualDirtyTracking() {
  [
    "manual-customer",
    "manual-site",
    "manual-asset",
    "manual-type",
    "manual-region",
    "manual-sla",
    "manual-risk",
    "manual-title",
    "manual-details",
    "manual-skills",
    "manual-top-k",
  ].forEach((id) => {
    const element = $(id);
    element.addEventListener("input", () => {
      manualFormDirty = true;
    });
    element.addEventListener("change", () => {
      manualFormDirty = true;
    });
  });
}

$("setup-button").addEventListener("click", setup);
$("refresh-button").addEventListener("click", () => refresh());
$("run-sample-button").addEventListener("click", runSample);
// requestSubmit() runs the browser's native constraint validation first, so
// the `required`, `min` and `max` attributes on the manual form are actually
// enforced. Wiring the button straight to runManual() bypassed all of them.
$("run-manual-button").addEventListener("click", () =>
  $("manual-job-form").requestSubmit()
);
$("manual-job-form").addEventListener("submit", runManual);
$("override-form").addEventListener("submit", submitOverride);
bindManualDirtyTracking();
$("dispatcher-identity").textContent = dispatcherId();
refresh();
