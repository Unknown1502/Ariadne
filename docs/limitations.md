# Limitations

What Ariadne does not establish. Stated in the same detail as what it does, because a system
that audits claims should be careful about its own.

## Scientific

**Behavioral faithfulness is not causal truth.** Ariadne measures whether a model's behavior
matches what its explanation predicted, under one declared intervention. A model could
produce the predicted behavior for reasons entirely unrelated to the stated one. Nothing here
recovers internal mechanism, and the schemas deliberately give no field in which to claim it.

**Counterfactual validity is domain-dependent.** "Neutralize this feature" assumes a neutral
value exists and that setting it produces a realistic input. In this laboratory both hold by
construction. In a real domain, neutralizing a feature can produce a case that could never
occur — and an effect measured on impossible inputs means little. The protocol version
records which convention was used; it does not make the convention correct.

**One protocol tests one thing.** These results are scoped to neutralization interventions.
A claim that survives neutralization might fail under ablation or substitution. `SUPPORTED`
means "under this protocol", and the scope on every verdict says so.

**Thresholds are choices.** The 0.10 minimum effect, 0.80 reproducibility, 0.90 validity, and
15% minimum perturbation are defensible defaults, not derived constants. They are on the plan
and recorded with every result, so a reader can disagree — and the benchmark shows the
verdict moving when they change, which is the honest way to present a threshold.

**The target model is synthetic.** Four hand-written formulas over three invented features.
This is deliberate — it is what makes the verifier's own accuracy measurable — but it means
zero external validity. Nothing here establishes clinical, financial, or legal performance,
and the laboratory is labelled as such in every surface that displays it.

## Operational

**Explanation Debt is not a scientific quantity.** It is a prioritization signal whose weights
encode an opinion about which explanation problems matter most. Every snapshot records its
policy version, and changing weights requires a new version, because two incomparable scores
that look comparable are worse than no score.

**Audit priority is a heuristic.** The additive scheme in `lineage-and-debt.md` is a
reasonable ordering, not an optimal allocation of audit budget. It has not been evaluated
against alternatives.

**Single laboratory, single claim shape.** The claim compiler handles subject-predicate-object
claims about feature influence. Explanations about interactions between features, about
thresholds, or about what the model did *not* consider are out of scope.

**Fixture sets are fixed.** Cases come from a declared distribution, which is what stops an
agent selecting data that flatters it — but it also means results are only as representative
as the distribution definition.

## Adversarial

**A model that detects probing defeats behavioral testing.** If the target behaves
differently under intervention than in production, every verdict here is about the probe
rather than the deployment. This is a fundamental limit of black-box behavioral auditing, and
no protocol design fixes it.

**Injection detection is pattern-based and therefore incomplete.** The real guarantee is
structural — a compromised Investigator still cannot write a verdict — but the detection
layer will miss novel phrasings, and the quarantine that depends on it will miss them too.

**Hash chains make tampering detectable, not impossible.** An attacker with database write
access can alter rows. `verify_integrity()` will report it; nothing prevents it.

## Engineering

**The cloud adapters have never run against Google Cloud.** `FirestoreRuntimeStore`,
`PubSubEventBus`, and `GeminiClient` are written and typed, and the Firestore store is
covered by the same contract suite as the local one - but against a client *double*. That
tests the adapter's logic, not network behaviour, consistency, permissions, or SDK version
drift. No cloud proof should be claimed until it has actually been deployed.

**Not integrated:** Vertex AI Agent Engine / ADK orchestration, Model Armor, Agent Gateway,
Memory Bank. The threat model documents in-repo equivalents and says plainly that no
integration exists. Claiming one that is not wired would be worse than the gap.

**Gemini's claim-extraction quality is unmeasured.** The benchmark runs on the offline
deterministic reasoner, so it evaluates the verifier and the protocol, not the language
model's semantic accuracy. Measuring that needs a labelled corpus of real explanations with
human agreement on what each one claims — genuine future work, and the most valuable next
step.

**Single-tenant, single-region, no authentication.** The API has no authn/authz. It is a
demonstration system.

**The local event bus is in-process.** It provides real at-least-once delivery, retries, and
dead-lettering, but a process restart loses queued events. The Pub/Sub adapter does not have
this property; the local one is for development and the demo.

## What would change my confidence

Stated so the claims here are falsifiable:

- **A real model, real explanations, human labels.** If the verifier's verdicts diverged from
  expert judgement on explanations people actually wrote, the protocol would need rethinking.
- **A domain where neutralization is ill-defined.** Would force the intervention vocabulary
  to become domain-specific rather than universal.
- **Evidence that lineage-based prioritization does not reduce audit cost.** The claim that
  memory makes auditing targeted rather than exhaustive is stated in the README and is
  currently unmeasured — it needs a cost model and a comparison against round-robin.
