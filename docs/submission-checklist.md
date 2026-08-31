# Release gate

Verified by running the thing, not by remembering that it used to work. Commands included so
each line can be re-checked.

## Scientific core

- [x] An explanation becomes a structured, testable Claim — `tests/unit/test_agents.py`
- [x] A Claim becomes an ExperimentPlan with constraints, seed, and thresholds
- [x] Interventions are validated *before* execution — `tests/unit/test_interventions.py`
- [x] Baseline, intervention, and control execute over paired fixture cases
- [x] Evidence carries input/output hashes and full provenance; it has no verdict field
- [x] The verifier returns exactly three verdicts, and all three occur in the benchmark
- [x] Ground truth is deterministic and published — formulas printed in the report
- [x] An invalid probe never produces SUPPORTED or CONTRADICTED

```bash
pytest tests/unit tests/integration -q
python -m benchmark.run_benchmark --fail-on-regression
```

## Fleet

- [x] Four roles with genuinely different write scopes — `assert_four_roles` raises on a fifth
- [x] Only the Verifier writes verdicts; only the Verifier uses no LLM
- [x] `AgentManifest` raises if a Verifier is declared with `uses_llm=True`
- [x] Typed, versioned handoffs; routing requires capability *and* payload schema
- [x] Malformed output is rejected, retried within budget, then quarantined
- [x] Tool calls are checked against the manifest and recorded either way
- [x] Each role has its own service account, in code and in Terraform
- [x] A worker crash resumes from checkpoint without duplicating evidence
- [x] Duplicate events produce no duplicate work

```bash
pytest tests/security tests/chaos -q
```

## Temporal system

- [x] v1/v2/v3/v4 lineage reconstructs correctly
- [x] History is append-only — the ledger exposes no update or delete
- [x] Hash chain detects an altered ancestor, a removed row, and a forged link
- [x] The chain stays linear when a batch of entries shares one timestamp — the case that
      used to fork it, and the reason `verify_chain` walks links instead of trusting row
      order — `tests/integration/test_lineage_chain_integrity.py`
- [x] Point-in-time reconstruction answers "what did we believe on day N?"
- [x] A distribution change expires evidence without rewriting it
- [x] Debt is decomposable, bounded, and carries its policy version
- [x] Changing debt weights requires a new policy version
- [x] Prior contradictions raise the next audit's priority

```bash
pytest tests/integration/test_lineage.py tests/integration/test_debt_and_governor.py -q
```

## Demo

- [x] No Analyze button exists anywhere in the console
- [x] A model-version event starts a full audit with no further user action
- [x] v1 CONTRADICTED, v2 SUPPORTED, v3 INCONCLUSIVE, v4 CONTRADICTED
- [x] Verdicts hold at every sample size tested (n ∈ 8…64)
- [x] Lineage, debt breakdown, and the approval gate are all visible
- [x] Duplicate-event safety is demonstrated live
- [x] The synthetic disclaimer is visible on screen at all times
- [x] The console labels the offline reasoner honestly — never a Gemini badge over a regex
- [x] A reset script produces an identical run

```bash
rm -rf var/demo && python -m backend.scripts.run_demo
pytest tests/integration/test_demo_narrative.py -q   # asserts the story, not just exit 0
```

The demo script used to be checked only for a zero exit code, which meant every number it
printed was unasserted — it could have told a different story with a green build. Its
transcript is now read and checked against the ledger rows the figures claim to come from.
Doing that is what surfaced the forked hash chain above.

## Honesty

The items most worth failing on:

- [x] No causal-truth claim anywhere — a test greps the whole backend for banned phrases
- [x] Every verdict carries a VersionScope; there is no way to construct one without it
- [x] Debt is labelled a policy score, not a scientific quantity, in code and in the UI
- [x] `INCONCLUSIVE` is reported, never hidden, and never folded into faithfulness
- [x] Unintegrated services (ADK, Model Armor, Agent Gateway, Memory Bank) are named as
      unintegrated rather than implied
- [x] The benchmark's floor reference is named `assume-faithful`, is stated to be the
      constant SUPPORTED, and its rate is printed beside the verdict mix that determines it —
      it is never quoted as the cost of trusting a model
- [x] Every ablation accuracy is reported with a 95% Wilson interval, and the doc states
      plainly that full vs no-control is **not** statistically distinguishable at n=14
- [x] SUPPORTED requires the bootstrap interval to exclude zero, not just a head-count
- [x] The experiment runner fails closed on model identity — an event for a model whose
      endpoint cannot be established produces FAILED with the reason, never a substituted
      run against a different model. Verified live; recorded as F11 in
      `docs/architecture-review.md`
- [x] Limitations are stated in the same detail as capabilities
- [x] Deviations from the design pack are documented with reasoning — `docs/decisions.md`

## Cloud

- [x] Deployed to a live Google Cloud project — `ariadne-12`, `asia-south1`: Cloud Run,
      Pub/Sub, Firestore, Cloud SQL. Console and API both serving.
- [x] The deployment found bugs an emulator could not — a missing `google_sql_user`, a Cloud
      Build substitution that only resolves inside step args, a missing
      `roles/pubsub.subscriber` grant, and an nginx proxy loop from forwarding the request
      `Host` header
- [x] Idempotency demonstrated against the live deployment — six already-processed events
      replayed, `duplicates_suppressed: 6`, `investigations_started: 0`
- [x] A real third-party model audited end to end — Gemini 2.5 **and** 3.5 Flash via Vertex
      AI, and the two versions disagree about the same explanation (`docs/real-model-audit.md`)
- [x] **Gemini as the Investigator** exercised live, which is a different surface from the
      audit above. `/api/v1/system` reports `provider: "gemini"`, a full investigation
      completes end to end through Vertex AI, and claim-extraction quality is now measured
      rather than declared unmeasured — primacy F1 0.143 for the keyword matcher against
      0.963 for Gemini (`docs/investigator-evaluation.md`)
- [x] Adversarial evaluation against live Gemini — 24 attacks, no false SUPPORTED observed,
      and the claim-compilation security boundary quantified (`docs/adversarial-evaluation.md`)
- [x] Model onboarding is real: register, connect, probe, declare output, validate against a
      real response, declare feature semantics, register an explanation source, and reach
      READY_FOR_VERIFICATION through six gates re-derived from live state

```bash
curl https://ariadne-api-uhcrowxnsq-el.a.run.app/api/v1/system
curl https://ariadne-api-uhcrowxnsq-el.a.run.app/api/v1/runtime
```

## Not done

Stated plainly rather than omitted:

- [ ] Four-minute video recorded — **the only mandatory submission artifact still missing**
- [ ] Technical blog post
- [ ] Public repository
- [ ] Frontend test framework — the console has no unit tests at all; its logic is covered
      by typecheck and by checking behaviour against the live API
- [ ] Pub/Sub **push** subscription — the worker's in-process pull loop is why the
      deployment needs `--min-instances=1 --no-cpu-throttling`. See `docs/limitations.md`.
- [ ] ADK, Model Armor, Agent Gateway, Memory Bank integrations — see `docs/decisions.md` §14

## Before submitting

- [ ] Re-read the official rules. Do not rely on any summary of them, including this one.
- [ ] Confirm every service claimed in the video is actually running — `/api/v1/system`
      reports the real configuration.
- [ ] Re-run `pytest` and the benchmark on a clean checkout.
