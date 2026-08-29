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

**Connecting a real model: the path exists, is tested, and has now been walked.**
`backend/experiment_engine/adapters.py` provides the integration seam — feature-space
contract, codec, transport, bounded retry, budget cap, caching, and replication — and
`docs/integrating-a-real-model.md` documents it. The load-bearing test runs the synthetic
laboratory model through the *entire* remote stack and requires the identical verdict and
reason codes, which is what makes "the adapter changes how the model is reached, not what is
measured" a checked claim rather than an assertion. Three things this genuinely resolves:

- **Cost** is bounded by construction. `BudgetedTargetModel` raises `BudgetExhausted` rather
  than silently running fewer cases than the plan declared — an experiment whose sample size
  contradicts its own record is worse than no experiment. (Building this surfaced a real bug:
  the runner wrapped every exception as retryable, so a budget exhaustion would have been
  retried, spending more money to reach the same certain failure. Fixed.)
- **Non-determinism** is measured rather than assumed away. `measure_noise_floor` samples a
  model's self-disagreement; `replicates_needed` converts that into the number of averaged
  calls an effect needs to clear its own noise floor — quadratic in the noise, so a model
  whose noise equals the effect being measured costs 16× to audit. That is the honest price,
  surfaced before the bill rather than after.
- **Unsound caching** is prevented, not documented. Caching a stochastic model would make the
  instability probe report perfect stability for a model that has none — a silent false
  negative on the gate protecting verdict integrity — so the adapter refuses to do it.

**It has now run against a live third-party model.** `backend/experiment_engine/gemini_target.py`
audits Gemini 2.5 Flash through Vertex AI, and `backend/scripts/probe_real_model.py` runs the
full onboarding-then-audit sequence against it. A real run (68 live calls) is recorded in
`docs/real-model-audit.md`. Two things that only a real model could have shown:

- **Gemini is measurably non-deterministic at temperature 0** — observed spread up to 0.165
  on identical inputs across repeated runs. The synthetic laboratory could never have
  surfaced this, and it is exactly the case `measure_noise_floor` / `replicates_needed`
  exist for.
- **The first live call hit `MAX_TOKENS`**, because Gemini 2.5 spends output tokens on
  internal reasoning before answering. Without the `finish_reason` check added earlier
  (F9), that would have surfaced as "malformed JSON" and sent the next person debugging the
  prompt instead of the token budget.

**What still does not resolve, and cannot:** a model that will not accept perturbed inputs
cannot be audited by *any* intervention-based method; a model that detects probing defeats
behavioural testing entirely; and `neutral_value` remains a domain judgement that no adapter
can supply (see "Counterfactual validity is domain-dependent" above). The Gemini run also
inherits every scientific limitation above — it is one model, one prompt shape, one
distribution, and a `SUPPORTED` verdict there means what it means everywhere else in this
system: true of that model version, on that data, under that intervention protocol.

## Operational

**Explanation Debt is not a scientific quantity.** It is a prioritization signal whose weights
encode an opinion about which explanation problems matter most. Every snapshot records its
policy version, and changing weights requires a new version, because two incomparable scores
that look comparable are worse than no score.

**Audit priority is a heuristic — now measured against the alternative.**
`benchmark/audit_priority_comparison.py` runs the real, unmodified `audit_priority()` against
real `LineageEntry` rows in a real ledger, across 20 independently seeded synthetic
populations of 200 claim families each, and compares it to round-robin scheduling under a
constrained per-round audit budget. Result: a mean 75.8% reduction in audits needed to
re-test every previously-contradicted family, with those families landing at the 10th
percentile of audit order under lineage priority versus an essentially uniform 49th under
round-robin (`var/audit-priority/audit-priority.md` after running it). What this does *not*
establish: that the additive scheme's specific weights (0.35 for a contradicted current
status, 0.20 for prior contradictions, and so on) are optimal — only that using lineage at
all beats using none. The population is synthetic by necessity (there is no real deployment
to sample from), and the comparison's own limitations are printed in its report rather than
asserted separately here.

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

**The cloud adapters have now run against emulators *and* against a deployed project.** This
line has been rewritten twice, and both rewrites are the point. It first said the Firestore
and Pub/Sub adapters were covered only by hand-written doubles — true, and the reason a real
defect (F9, below) survived 675 passing tests: the double raised whatever exception the
adapter's code happened to expect, so a mismatch between the two could never surface. It
then said they had run against emulators but never a deployed project. That is also no
longer true, and leaving it would have been the same failure in the opposite direction —
a document understating its own system is still a document that is wrong.

`tests/integration/test_firestore_emulator.py` and `tests/integration/test_pubsub_emulator.py`
now run `FirestoreRuntimeStore` and `PubSubEventBus` against
`gcr.io/google.com/cloudsdktool/cloud-sdk:emulators` — the real `google-cloud-firestore` and
`google-cloud-pubsub` wire protocols, not a double. This covers the class of bug a double
structurally cannot catch: does the adapter's code guess the real client library's behaviour
correctly? Every method on `PubSubEventBus` that carried a `# pragma: no cover - needs GCP`
comment — `publish`, the streaming-pull callback, ack, nack, `publish_duplicate` — has now
executed against a real broker, including the exact scenario F9 was found in: two
`FirestoreRuntimeStore` instances racing `claim()` on an identical idempotency key.

Both test files are skipped unless `FIRESTORE_EMULATOR_HOST` / `PUBSUB_EMULATOR_HOST` are
set, so the default suite — the one that must pass with no Google account — is unaffected;
run them explicitly to reproduce (commands are in each file's docstring).

**Then it was deployed, and the emulator's blind spots turned out to be real.** Project
`ariadne-12`, `asia-south1`: Cloud Run, Pub/Sub, Firestore, and Cloud SQL, with
`/api/v1/system` reporting the live wiring. The two failures that found were exactly the
two an emulator cannot model:

- **IAM.** The worker's service account was missing `roles/pubsub.subscriber`. Publishing
  returned 200 and the message then sat in the subscription forever. Under an emulator there
  are no permissions to get wrong, so nothing had ever exercised that boundary — and the
  demo's headline claim ("the worker wakes up with nobody clicking Analyze") was false in
  the only environment that counted.
- **Cost and lifecycle**, in the scale-to-zero tension documented below.

**What is still not established:** multi-region consistency, quota and rate-limit behaviour
under load, and network partition handling. The deployment is single-region and has served
one demo's worth of traffic, which is not evidence about any of those.

**Gemini as the *agent* LLM remains genuinely unverified — which is not the same Gemini the
Scientific section reports auditing.** Two separate surfaces, and conflating them would let
one real result cover for an untested path:

| | what it is | status |
|---|---|---|
| `experiment_engine/gemini_target.py` | Gemini as the **target** being audited | 68 live Vertex AI calls, recorded in `docs/real-model-audit.md` |
| `agents/llm.py` (`GeminiClient`) | Gemini as the **Investigator** proposing experiments | never called live |

The deployed system reports `reasoner.provider = "stub"` on `/api/v1/system` — the offline
deterministic reasoner — so the live demo has never exercised `GeminiClient` either. It
being the honest answer is exactly why that endpoint exists.

There is no equivalent emulator for the Gemini API.
`GeminiClient` was instead checked against the live `google-genai` SDK documentation and
exception model (see F9), and `tests/unit/test_gemini_response_handling.py` exercises its
`finish_reason` / `prompt_feedback` handling against doubles shaped like real SDK response
objects. Neither of those is a live model call. That gap is real and stays open until someone
runs it with `LLM_PROVIDER=gemini` and a Google API key.

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

**Scale-to-zero and the always-listening worker are in real tension, found by actually
deploying.** `AriadneWorker` starts its Pub/Sub streaming-pull subscriber inside the Cloud
Run service's `lifespan`, in-process with the API. That works while a request is in flight;
Cloud Run's default behaviour freezes CPU once a response is sent and, with
`min_instance_count = 0`, can tear the whole container down between requests. The very first
live deployment reproduced this exactly: `POST /api/v1/events/model-version-deployed`
returned 200 (the publish succeeded), but the message sat in the subscription indefinitely -
nothing ever appeared in `/api/v1/investigations`, because the instance that was supposed to
pull it back off Pub/Sub had already been reclaimed. Setting `--no-cpu-throttling
--min-instances=1` fixed it immediately and reproducibly. That keeps the demo's headline
claim - "the worker wakes up with no one clicking Analyze" - literally true only when an
instance is kept warm, which is a real, ongoing cost the "scale to zero" story in
`docs/deployment.md` does not currently account for. The architecturally correct fix is a
Pub/Sub **push** subscription that delivers into a dedicated HTTP endpoint (so processing
happens inside a request Cloud Run is already allocating CPU for, restoring true
scale-to-zero) rather than a background pull loop; that is a real code change, not done here.
Terraform's own default (`min_instance_count = 0`) is left as the honest baseline: a
deployment that needs to actually process events reliably must set `--min-instances=1` and
`--no-cpu-throttling`, and pay for it.

**The hash chain forked whenever two entries shared a timestamp, and nothing noticed for
the same reason nothing ever notices: no test looked.** `expire_evidence` writes one EXPIRES
row per affected reading, all stamped with one `moment`, and re-read "the previous entry" as
`lineage_for_family(...)[-1]`. That query orders by `(created_at, id)`; with `created_at`
tied across the batch the tiebreak fell to `id` — a *content-addressed hash*. "Last" meant
"largest hash", so every row after the first linked back to the same predecessor. One
distribution shift produced four entries claiming one parent, three unreachable from the
origin.

It failed in both directions at once. `verify_chain` compared links against sort order and
reported an untouched history as **broken** — an integrity check crying wolf is an integrity
check people stop reading. And the chain really had forked, so a whole branch could be
dropped and the walk would never miss it: tamper-evidence quietly weakened in the one
structure this project points at when it says history cannot be rewritten.

Every existing chain test spaced its audits thirty days apart, where the tie cannot happen.
What surfaced it was finally asserting `run_demo.py`'s output instead of only its exit code
— the script had been printing `Hash chain intact: True` truthfully, because it prints that
line *before* the distribution shift. Fixed: the tail is read from the links (the entry
nothing points at) rather than from a sort, `verify_chain` walks the chain instead of
trusting row order, and `tests/integration/test_lineage_chain_integrity.py` collapses the
timestamps deliberately. Eleven of its tests fail against the old code.

**A partial run left append-only-detectable debris, on purpose.** The first (broken, no
subscriber-permission) deployment attempt let one investigation start and fail partway
through. A later, correctly-processed redelivery of a similarly-shaped event collided with
that partial state and was correctly rejected by the append-only guard
(`AppendOnlyViolation: claims.CLM-... already exists with different content`) rather than
silently overwriting it. That is the guard doing its job, not a defect - but it is worth
naming as a real, observed failure mode: a worker that dies mid-investigation after writing
some but not all of its records can leave a claim family in a state where a *legitimately
different* later attempt at the same content-addressed ID is refused. Recovering from that
currently means creating a new claim family or accepting the FAILED state; there is no
automated "abandon this dead partial investigation and let a fresh attempt reuse its
address" path yet.

## What would change my confidence

Stated so the claims here are falsifiable:

- **A real model, real explanations, human labels.** If the verifier's verdicts diverged from
  expert judgement on explanations people actually wrote, the protocol would need rethinking.
- **A domain where neutralization is ill-defined.** Would force the intervention vocabulary
  to become domain-specific rather than universal.
- ~~Evidence that lineage-based prioritization does not reduce audit cost.~~ **Measured.**
  `benchmark/audit_priority_comparison.py` found a mean 75.8% reduction in audits needed
  versus round-robin, across 20 seeded populations — see the "Operational" section above.
  What would still change this: a real audit-cost model with per-claim business impact,
  which the current comparison has no notion of, or evidence that the synthetic outcome
  mix (65% supported / 20% contradicted / 15% inconclusive) doesn't resemble anything a real
  deployment would produce.
