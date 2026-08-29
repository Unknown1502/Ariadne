# Engineering decisions

Where this build departs from the design pack in `ARIADNE_HACKATHON_STARTER_PACK/docs` and
`ARIADNE_HACKATHON_DEEP_CODING_PROMPTS`, and why. Silent deviations are how a spec stops
meaning anything, so each one is recorded with the reasoning.

---

## 1. The starter verifier could not produce the required demo results

**Design pack:** the starter pack's own `experiment_engine/verification.py` ships a `verify()`
whose rules
are `SUPPORTED` when `expected_observed and control_effect <= abs(effect)`, `CONTRADICTED`
when validity is high and the effect is absent, `INCONCLUSIVE` otherwise.

**Problem:** for v1 — the demo's flagship contradiction — the urgency effect *is* observable
at a low threshold while the control is stronger. That combination falls through to the
`else` branch and returns `INCONCLUSIVE`. The demo and the README both require
`CONTRADICTED`.

**Decision:** the verifier was written from the methodology docs rather than extended from
that file. The README's own definition is the correct one and the placeholder did not
implement it: *"CONTRADICTED: valid intervention failed to produce the predicted effect, **or
a control feature produced a stronger effect**."* Control dominance is now a first-class
contradiction path, gated on the claim asserting primacy.

---

## 2. The debt example in the design pack is internally inconsistent

**Design pack:** `04-explanation-debt.md` weights contradictions at 25% of a 100-point score,
then shows a worked example with `Contradictions: +31`.

**Decision:** follow the weight table; report computed values. A demo shows whatever the
evidence produces, typically 25–55 across the four-version run.

**Why:** reverse-engineering the illustrative total would mean hardcoding a number the
evidence does not support — the specific thing the operating contract forbids. The
discrepancy is noted in `docs/lineage-and-debt.md` rather than quietly resolved.

---

## 3. v3.0.0 had to actually be uncertain

**Design pack:** prompt 03 requires v3 to be INCONCLUSIVE, but the illustrative coefficients
(`0.50u + 0.45c + 0.05b`) are deterministic and yield a clean, reproducible effect — which
verifies as SUPPORTED.

**Decision:** v3 keeps those weights and adds a seeded perturbation, `N(0, 0.10)` keyed by
the input vector itself. The result is a *rough response surface*, not a random one:
`predict` stays a pure function, so the same input always gives the same score, while
different cases disagree with each other.

**Effect:** the urgency effect clears the threshold on ~58% of cases — neither reproducibly
present nor reproducibly absent. That is a genuine INCONCLUSIVE, arrived at by the rules
rather than asserted.

Run-to-run instability is a *different* failure, so `UnstableTriageModel` exists as a test
double to prove the verifier catches that too. It is never registered as a real version.

---

## 4. Model coefficients and the baseline distribution were tuned for robustness

**Design pack:** v1 is `0.25u + 0.70c + 0.05b`; the baseline urgency range is unspecified.

**Problem:** with urgency drawn from U(0.60, 0.95), v2's reproducibility landed at 0.833
against a 0.80 threshold. A demo result with a 3-point margin is a demo result that flips
when someone changes `repetitions`.

**Decision:** v1 became `0.20u + 0.75c + 0.05b`, v4's main effect dropped to `0.10`, and the
baseline urgency range narrowed to U(0.65, 0.95) so every case is a genuinely large
perturbation.

**Verified:** all four verdicts now hold at n ∈ {8, 12, 16, 24, 32, 64}, asserted by a
parametrized test. v1 sits at 0.000 observed, v2 at 1.000, v3 between 0.375 and 0.641, v4 at
≤0.016. A result that flips with sample size is not a result.

---

## 5. Benchmark baselines are ablations, not strawmen

**Design pack:** prompt 13 asks for comparison against "a fixed test suite", "single-agent
orchestration", and "no-lineage mode".

**Decision:** compare `full` against `no-control`, `no-validity`, and `self-report` — the
same code with exactly one mechanism removed.

**Why:** to score a "single-agent LLM workflow" honestly, one would have to build it and run
it. Simulating one with a stub that always answers SUPPORTED and then reporting its false-
support rate would be fabricating a measurement, and the resulting numbers would describe my
stub rather than anything real.

Ablations avoid that entirely: identical code, identical fixtures, one variable. Each answers
a question a reviewer should ask — and each mechanism demonstrably earns its place
(no-control costs 1 false support; no-validity costs 3 false contradictions).

`self-report` is a fixed rule that reads the target model's own explanation and concludes
SUPPORTED because that is what the explanation asserts. It is labelled in the report as *not*
a language model, and makes no claim about how one would behave.

---

## 6. `ExperimentPlan` gained a required `created_at`

**Found by:** the ledger refusing to store a plan, because `_appended_at` could not find a
timestamp on it.

**Decision:** add it as required rather than optional. A plan is a persisted record, and a
time-ordered evidence ledger cannot hold a row with no time.

---

## 7. Ledger identity excludes volatile metadata

**Found by:** re-auditing the same scope raised `AppendOnlyViolation`. The evidence was
byte-identical — deterministic experiment, same seed — but `created_at` differed.

**Decision:** duplicate detection compares an identity hash that excludes `created_at`,
`computed_at`, `executed_at`, and `duration_ms`.

**Why:** those fields record *when* an observation was made, not *what* was observed. Two
executions of the same deterministic plan differ in exactly them. Without the exclusion,
at-least-once delivery produces spurious tamper alarms; with it, anything beyond those four
fields is still a genuine conflict and still raises.

The list is deliberately short and explicit rather than heuristic, since each entry weakens
the integrity guarantee slightly and should be visible in a diff.

---

## 8. Pipeline steps check for their artifact, not just the state

**Found by:** a crash-recovery test. Resuming from a rolled-back state re-ran the debt
calculation, whose `previous_total` now included the snapshot just written — same ID,
different content, `AppendOnlyViolation`.

**Decision:** each step short-circuits on *either* the state having passed it *or* the
artifact already being recorded on the investigation.

**Why:** the artifact check is the one that matters. A rolled-back state with a durable
artifact still present is exactly what a crash leaves behind, and re-deriving the artifact
would rewrite history.

---

## 9. Event identity includes the distribution version

**Found by:** an end-to-end run where a re-audit under shifted data silently did nothing.

**Cause:** the idempotency key derives from `(event_type, aggregate_id, aggregate_version)`,
and `aggregate_version` was just the model version. "v2.0.0 on the original data" and
"v2.0.0 on the shifted data" collided, so the second — genuinely different — audit was
skipped as a duplicate.

**Decision:** `aggregate_version` is now `{model_version}@{distribution_version}`.

This is the sharpest lesson in the build: idempotency keys are a *semantic* decision. A key
that is too coarse does not merely lose an event, it loses one while reporting success.

---

## 10. A vague explanation returns "untestable", not a verdict

**Design pack:** prompt 04 lists "vague explanation" as a case to handle, and prompt 06 has
the verifier treat low testability as INCONCLUSIVE.

**Problem:** "several factors contributed" names no feature at all, so no claim can be
constructed — the pipeline crashed on the retry budget.

**Decision:** a new non-retryable `UntestableExplanation`, distinct from
`AgentOutputError`. The latter means the model failed to comply and a retry might help; the
former means the model *did* comply and correctly reported that the explanation states no
hypothesis.

**Why not retry:** retrying would only pressure the model into naming a driver nobody
claimed. The investigation ends with `CLAIM_EXTRACTION_FAILED` and no verdict, which is the
honest outcome.

---

## 11. `SerializeAsAny` on event payloads

**Found by:** a payload serializing to `{"schema_version": "1.0"}` with no warning.

**Cause:** Pydantic v2 serializes against the *declared* field type. With
`payload: EventPayload`, every subclass field was silently stripped on the way to the wire.

**Decision:** `payload: SerializeAsAny[EventPayload]`, plus a round-trip test.

This one is worth flagging to anyone building event-driven systems on Pydantic v2: the
failure is silent, and it destroys every event's contents.

---

## 12. No Recharts or React Flow in the console

**Design pack:** `frontend/README.md` recommends React, TypeScript, Vite, Tailwind, React
Flow, and Recharts.

**Decision:** React + TypeScript + Vite, with hand-written CSS and inline SVG. No Tailwind,
no chart library.

**Why:** prompt 12 says to build "an investigation canvas, not a generic admin dashboard",
and to avoid "unexplained charts". Chart libraries carry a house style that pulls directly
toward the thing being warned against. The three graphics needed — a per-case delta strip
plot, a lineage strip, and debt component bars — are simple SVG, and the delta plot in
particular has to be shaped around the effect threshold in a way a generic bar chart cannot
express.

The delta plot is the design's central bet: showing every individual case rather than a
summary statistic is what makes "v1's effect never reaches the threshold" something you see
instead of something you are told.

---

## 13. `RunSummary` tolerates floating-point error

**Found by:** `mean([0.7, 0.7, 0.7]) == 0.7000000000000001`, which failed a
`minimum <= mean <= maximum` check on legitimate data.

**Decision:** a 1e-9 tolerance, documented as a sanity guard against transposed or fabricated
summaries rather than a numerics test. The tolerance is far below any effect size Ariadne
can measure.

---

## 14. Deferred deliberately

Named rather than half-built, per the design pack's own "cut if late" list:

- **Vertex AI Agent Engine / ADK orchestration.** The agent boundaries, typed handoffs,
  manifests, and permission checks are all implemented in-repo. Wiring ADK's runner would
  change how agents are *invoked*, not what they are allowed to do.
- **Model Armor / Agent Gateway.** The threat model documents the equivalent in-repo
  controls and says plainly that no integration exists.
- **Memory Bank.** Lineage already serves as evidence-backed memory, with the property Memory
  Bank guidance asks for: every summary references evidence IDs.
- **Multi-domain support.** One laboratory, done properly.

---

## 15. `RUNTIME_STORE=firestore` used to lie

**Found by:** auditing which cloud adapters actually existed, rather than which ones the
config accepted.

**Cause:** `Settings` validated `runtime_store="firestore"`, but `open_runtime_store()`
returned `LocalRuntimeStore` unconditionally. Setting the flag gave you local JSON files
while `/api/v1/system` reported `runtime_store: firestore` to the console - a false
cloud-proof claim generated by the system that exists to argue against false claims.

**Decision:** write the real `FirestoreRuntimeStore`, and make the factory dispatch. A
silent fallback is worse than a hard failure here, because the failure is visible and the
fallback is not.

**Two shapes worth noting.** Runs live in a subcollection (`runs/{experiment_id}/items/`)
rather than a flat collection with a query - no composite index to create, and no risk of a
partial index returning a *subset* of completed runs, which would silently re-execute work.
And `record_run` writes a small marker on the parent document, because Firestore does not
return subcollection-parent documents from `stream()`; without it the checkpoint count reads
as zero no matter how many runs are stored. The test double reproduces that behaviour, which
is how the bug was caught before deployment rather than after.

**Testing:** both stores now run through one parametrized contract suite - 74 assertions,
each executed twice - so "the cloud path behaves like the local one" is tested rather than
assumed. The Firestore store runs against a client double, which covers the adapter's logic
and *not* real Firestore. That boundary is stated in `tests/fakes.py` and in
`docs/limitations.md`.

---

## 16. Configuration must do something or not exist

**Found by:** a script over `config.py` asking which declared settings any code reads.
Eleven of thirty-two were read by nothing - the third occurrence of the pattern behind
findings F1 and F2 in `docs/architecture-review.md`.

**Decisions, by category:**

- **Ceilings, not overrides.** `AGENT_LOOP_BUDGET` and `AGENT_TIMEOUT_SECONDS` tighten every
  manifest and never loosen one. Per-agent budgets encode real reasoning (the Verifier gets
  one attempt because a deterministic computation that failed once fails identically), and a
  global value that raised them would silently erase it.
- **Two staleness thresholds, deliberately.** `EVIDENCE_VALIDITY_DAYS` drives operational
  freshness - audit priority and staleness. Debt scoring keeps `Policy.thresholds.stale_days`,
  which is versioned, because debt has to stay comparable across deployments and a per-
  deployment knob would break that. Both are documented where they are declared.
- **`GEMINI_TEMPERATURE` deleted.** Every agent pins 0.0 so the same explanation compiles to
  the same claim. A setting whose only effect is to break a design guarantee should not be
  offered, and offering one that agents then ignore is worse.
- **Unbuilt capabilities fail startup.** `ENABLE_MEMORY_BANK`, `ENABLE_AGENT_GATEWAY`,
  `ENABLE_MODEL_ARMOR`, and `CLOUD_STORAGE_BUCKET` now raise with a message naming what was
  requested. They were already documented as unintegrated; a flag that accepts `true` and
  does nothing converts an honest gap into a quiet false claim.

**Automated.** `tests/unit/test_config_is_honest.py` fails if any setting is read by nothing.
A check that has to be remembered will be forgotten - this one was, twice.

