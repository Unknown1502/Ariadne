# Four-minute demo

Everything below is produced by running the system. No slide contains a number that the
code does not compute.

**Reset before every rehearsal:**

```bash
rm -rf var/demo && python -m backend.scripts.run_demo
```

The script prints the whole narrative and exits 0. It is also the backup recording: if the
live console fails, the terminal tells the same story with the same numbers.

---

## 0:00 — The unlikely hero

> A triage nurse is told: **HIGH PRIORITY**, because *"Urgency marker was the primary
> driver."* She is not an ML engineer. She has no way to check that.

Show the console hero. The decision and the explanation, plainly.

**Say:** "Ariadne's job is to find out whether that reason deserves trust."

---

## 0:20 — The laboratory is open

Show the four formulas from the model registry panel.

**Say:** "The target model is synthetic, and that is deliberate. The formula is printed on
the page, so what a faithful explanation would be is arithmetic rather than opinion — which
is the only way to measure whether the auditor itself is right."

**Point out:** the same explanation ships with all four versions. Only the model changes.

---

## 0:45 — Compile the claim, run the probe

Click **deploy v1.0.0**. Do not click anything else.

The claim card appears:

```
IF   urgency_marker is_primary_driver of priority_score
THEN neutralizing urgency_marker while preserving signal_b, signal_c
SHOULD decrease the priority_score
```

Then the experiment: baseline, intervention, control on `signal_c`, seed `20260101`.

---

## 1:20 — The evidence, visible

Show the delta plot. Every line is one case; the dashed rule is the effect threshold.

**Say:** "Every case falls short of the threshold. And the lower band is the control —
neutralizing a feature the explanation never mentioned moves the score three times as much."

```
VERDICT: CONTRADICTED
effect −0.055 · control −0.161 · reproducibility 0.00 · validity 1.00
CONTROL_DOMINATES · EFFECT_REPRODUCIBLY_ABSENT · PRIMACY_REFUTED
```

**Say:** "Gemini proposed the test. Deterministic code decided the result. The verifier has
no language model in it at all."

---

## 1:40 — A version ships. Nobody clicks Analyze.

Click **deploy v2.0.0**, then take your hands off the keyboard.

**Say:** "That published an event — the same event a model registry publishes on deploy.
Nothing on this screen runs an analysis. A background worker picks it up."

Wait. The investigation appears on its own.

```
VERDICT: SUPPORTED
effect −0.220 · control −0.032 · reproducibility 1.00
```

**Say:** "Same sentence. Different model. Now it's true — and the control confirms urgency
really is the stronger driver this time."

---

## 2:10 — v3 and v4

Deploy both.

```
v3.0.0  INCONCLUSIVE   effect clears the threshold on 58% of cases
v4.0.0  CONTRADICTED   urgency acts only through an interaction
```

**Say on v3:** "This is the answer most systems refuse to give. The evidence is genuinely
mixed, so the honest output is 'we don't know'. Manufacturing a verdict here would be the
easiest way to look confident and be wrong."

---

## 2:30 — The claim's history

Show the lineage strip: `v1 ✕ — v2 ✓ — v3 ? — v4 ✕`, hash chain intact, audit priority 0.85.

**Say:** "Append-only. The v2 result was never edited when v3 disagreed — it's still there,
still true of v2. And prior contradictions raise this claim's audit priority, which is how
Ariadne decides what to re-test first instead of sweeping everything."

If time allows, show point-in-time reconstruction from the demo script output.

---

## 3:10 — The data drifts

Click **distribution shift**.

**Say:** "Input data moved. Urgency now clusters near its own neutral value."

Show: current evidence becomes **none**. The four readings are marked expired, not deleted.

Re-audit v2 on the new distribution:

```
VERDICT: INCONCLUSIVE
INVALID_INTERVENTION · WEAK_PERTURBATION · validity 0.25
```

**Say:** "This is the most important slide. The probe can no longer move the input enough to
test the claim — so Ariadne says INCONCLUSIVE, not CONTRADICTED. A system that reported a
refutation here would be manufacturing one. Our benchmark measures exactly this: remove that
check and you get three false contradictions out of fourteen cases."

---

## 3:20 — Debt and the Governor

```
Explanation Debt: 50 / 100
  Inconclusive              +20
  Version inconsistency     +15
  Distribution sensitivity  +15
```

**Say:** "Decomposable — every component shows its ratio, weight, and the claims behind it.
And it's honestly labelled: this is an operational risk score whose weights are a policy
choice, not a scientific quantity. Every snapshot records the policy version."

Show the Governor requiring human review, with a pending approval.

**Say:** "Two contradictions on one claim. The Governor escalated to a human — and it hasn't
executed anything. That request is a gate, not a notification."

---

## 3:40 — Fleet resilience

Click **send a duplicate event**.

Show the runtime panel: duplicates suppressed climbs, ledger row counts do not move.

**Say:** "At-least-once delivery. Three duplicates, zero duplicate experiments — the worker
claims an idempotency key before doing any work."

Point at ledger integrity: *every row hash recomputes*.

---

## 3:50 — What you can check

Show the fleet table. Four roles, four different write scopes.

**Say:** "Only the Verifier can write a verdict, and it's the one role that uses no language
model. That isn't a policy — the manifest raises if you try to declare a Verifier with an
LLM."

---

## 4:00 — Close

> "Ariadne makes explanations prove themselves — across time, model versions, and changing
> data. It measures behavioral faithfulness under a declared protocol. It does not claim
> causal truth, and it isn't a clinical system."

---

## Rehearsal notes

- Reset between every run. Stale state changes debt totals and breaks the narrative.
- The v3 INCONCLUSIVE is the strongest moment. Do not rush it — it is what separates this
  from a pass/fail test suite.
- If asked *"isn't this just counterfactual testing?"*: counterfactual testing is one
  mechanism inside this. What is on screen is claim compilation, validity gating, temporal
  lineage, debt, and policy-driven re-audit — and the demo just showed the same claim getting
  four different correct answers.
- If asked *"is Gemini decorative?"*: turn it around. The Investigator distinguishes "the
  primary driver" from "contributed", and those compile to claims that the same evidence
  resolves differently. Two benchmark cases demonstrate it.
- **Never claim a Google Cloud service is running unless it is.** The honesty bar reports
  the real configuration; if it says `local`, say local.
- If the offline reasoner is in use, say so. The console labels it, and it will be visible.
