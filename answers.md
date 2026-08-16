# Part 3 — Engineering Reasoning and AI Governance

Implemented claims name the file. Everything else is design intent.

---

## 1. AI Authority & Risk

The question is not how accurate the model is but what being wrong costs and who
absorbs it. Dispatch is two-sided: a bad assignment costs the customer an SLA
breach and costs a vendor revenue they were entitled to compete for. Different
victims, different remedies — so a single accuracy threshold is the wrong
control.

Four categories should never auto-dispatch. **Safety exposure** (stuck lift, gas
or electrical fault, no heat on an occupied site): the model has no
representation of "someone could be hurt if this is slow", and the error is not
bounded by job value. **Thin evidence**: the subtle failure is a vendor scoring
highly because their three completed jobs went well. **A ranking that isn't
decisive**: if rank 1 beats rank 2 by half a point the system has found two
acceptable vendors and broken a tie on noise; spending the SLA on a coin flip
while presenting it as a recommendation is worse than saying so. **Anything that
changes the rules rather than applying them** — policy exceptions, contractual
overrides, or any decision that systematically shifts work between vendors. That
last is the fairness boundary, and no per-job confidence score can detect it.

Implemented: `HUMAN_APPROVAL_RISK_LEVELS` forces review on high-risk jobs
whatever the score; confidence is gated by requirement fit and evidence quality
and never reads the score; `MARGIN_THRESHOLD` catches ties. `service.py` derives
status from `review_reasons` rather than from the score, so an empty list is the
only auto-dispatch condition and a new policy cannot be forgotten. Autonomy
should still be earned incrementally: advisory-only first, measure acceptance,
then automate only the narrow slice where acceptance is already very high.

---

## 2. Model Drift & Feedback

Three failure modes that look nothing alike. **Input drift**: distribution of
each `ScoreFactors` component, vendor and region mix, rate of missing or stale
profile data — catches "a large vendor churned" long before outcomes move.
**Output drift**: score distribution, abstention rate, margin distribution. A
falling abstention rate is not good news; it usually means an upstream field
started arriving with more confident-looking values. **Outcome drift**:
acceptance, override rate, SLA attainment, rework — sliced by region, job type
and risk, because aggregates hide the cases that matter. Two references are
needed, not one: the previous model *and* the `rules-v1` baseline, scored in
shadow, so "fall back" stays a visible option.

Overrides are the dangerous part. An override says a human disagreed; it does
not say they were right. The dispatcher may have had context the system lacks,
may be applying a policy that should be a rule rather than a learned preference,
may be exercising a bias that training would launder into the model — or may
simply have been wrong, with the overridden job finishing late. So: capture the
override with the original recommendation, chosen vendor, actor, reason and
model version (implemented — every override records all of it), then **wait for
the outcome**. The label is what happened, not what was chosen. Overrides find
and weight interesting cases; they are not ground truth. Recurring reason codes
usually mean a missing rule, and the fix is a rule, not training rows.

Then the safety ladder: versioned training snapshot, a time-based holdout (never
random — it leaks the future and makes any dispatch model look excellent), slice
and fairness evaluation, shadow, canary with rollback on business metrics rather
than offline loss. The blend is bounded and sits behind the eligibility gate, so
a bad model degrades ranking rather than dispatching an ineligible vendor.

Second-order risk: recommendations create their own training data. A
highly-ranked vendor gets more jobs, accumulates more history and looks better
still, while a new vendor never accumulates evidence — this converges on a
monopoly. Mitigation is deliberate exploration with those jobs flagged. Not
implemented, but `sample_size` and `data_age_hours` exist so the starvation is
visible.

Currently implemented: one structured JSON log line per decision carrying
`model_version`, `decision_state`, `top_confidence`, `decision_margin` and
`review_reason_count`, so the distributions above are Logs Insights queries on
day one. Alerting is designed, not built.

---

## 3. Data Quality & Events

Every event needs a stable `event_id`, type, **schema version**, `job_id`, a
correlation ID spanning the decision, the producer, and `occurred_at` kept
distinct from `received_at`. The gap between those timestamps is the most useful
data-quality metric available: it turns "the recommendation was wrong" into "the
capacity snapshot was forty minutes stale".

The feature snapshot must be versioned with the decision. Storing "we
recommended V-001" is nearly useless; storing the `ScoreFactors` that produced
it, the `model_version`, and the age of the underlying data lets you separate a
bad model from bad input. Implemented — every recommendation carries its factor
breakdown, `model_version`, `requirement_fit` and `evidence_strength` into the
audit trail. Freshness is a first-class field for the same reason:
`data_age_hours` and `sample_size` make a stale or thin profile visibly lower
confidence instead of silently producing an overconfident answer.

The outcome tail is where most systems are thin and is what section 2 depends
on: recommendation generated, accepted or overridden with reason and actor,
vendor assigned, accepted or declined, work started, completed, SLA met or
missed, rework, cancellation. Without it there is no label and no retraining.
At the boundary: validate and reject early; range-check impossible values rather
than clamping, because clamping hides the upstream bug forever; quarantine
malformed events with the raw payload retained; treat event lag, duplicate rate
and DLQ depth as data-quality signals, not infrastructure noise.

Consumers must be idempotent — at-least-once delivery is not a rare edge case.
Implemented: `app/idempotency.py` claims each `event_id` with an S3 `PutObject`
using `IfNoneMatch: "*"`, an atomic compare-and-set, before any side effect. One
caveat on the current build: the vendor pool arrives in the request rather than
from a system of record, so the service holds no authoritative vendor data and
cannot detect a stale caller snapshot. In production that lookup belongs behind
the scoring service.

---

## 4. Failure Modes

A dispatch system that stops dispatching has failed worse than one dispatching
without AI assistance. Jobs are real and customers are waiting; the AI is an
accelerator, not a prerequisite. Every failure path must end with a human able
to act.

**Unavailable** — fall back to the deterministic scorer, which shares no
infrastructure with the model path; that independence is the reason to keep
`rules-v1` alive rather than treat it as legacy. Implemented: a missing or
invalid artifact logs `model.artifact_rejected` and continues on rules. If the
whole service is down, degrade to an unranked eligible-vendor list and a manual
queue, tagging those jobs so the outage cost is measurable. Fallback must never
be silent — an operator acting on a degraded recommendation has to know it is
degraded. **Slow** — hard timeout well inside the dispatcher's tolerance, treat
a breach as unavailability, retry only idempotent operations with jittered
backoff and a circuit breaker. Do not wait: a recommendation arriving after the
dispatcher has moved on has negative value, because it invites second-guessing a
decision already made.

**Low confidence** — do not auto-assign, but do not withhold the analysis
either. Return `manual_review_required` *with* the candidates, factors,
rationale and the specific reasons a human is needed. "The model is unsure" is
not actionable; "the top candidate has 11 completed jobs on record and the data
is four days old" is. An abstention should read like a colleague explaining
their hesitation, not an error state.

Two things the audit trail depends on. The audit write must not be best-effort:
serving a recommendation whose record failed to write means making an
unreviewable decision, which is worse than making none. And a failed run must
surface as failed — a real defect here was the worker looking up the run trace
by SQS `messageId` while traces are keyed by `event_id`, so the lookup always
missed and the browser polled a trace stuck at "running" until it timed out,
showing a generic timeout for a specific, diagnosable error. Fixed in
`lambda_function.py`; the lesson is that an error path with no test is an error
path that does not work.

Not implemented: circuit breakers, WAF, throttling, and CloudWatch alarms on
errors, DLQ depth and age-of-oldest-message. Tracked in [`STATUS.md`](STATUS.md).
