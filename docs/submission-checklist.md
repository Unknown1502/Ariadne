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
- [x] Hash chain detects an altered ancestor
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
```

## Honesty

The items most worth failing on:

- [x] No causal-truth claim anywhere — a test greps the whole backend for banned phrases
- [x] Every verdict carries a VersionScope; there is no way to construct one without it
- [x] Debt is labelled a policy score, not a scientific quantity, in code and in the UI
- [x] `INCONCLUSIVE` is reported, never hidden, and never folded into faithfulness
- [x] Unintegrated services (ADK, Model Armor, Agent Gateway, Memory Bank) are named as
      unintegrated rather than implied
- [x] The benchmark's `self-report` baseline is labelled as a fixed rule, not a language model
- [x] Limitations are stated in the same detail as capabilities
- [x] Deviations from the design pack are documented with reasoning — `docs/decisions.md`

## Not done

Stated plainly rather than omitted:

- [ ] Deployed to a live Google Cloud project — Terraform and Cloud Build are written and
      the adapters exist, but nothing has been applied. No cloud proof should be claimed
      until it has.
- [ ] Gemini path exercised against the real API — the code is written and typed; the
      benchmark runs on the offline reasoner, so Gemini's claim-extraction quality is
      unmeasured.
- [ ] Four-minute video recorded
- [ ] Technical blog post
- [ ] Public repository and hosted URL
- [ ] ADK, Model Armor, Agent Gateway, Memory Bank integrations — see `docs/decisions.md` §14

## Before submitting

- [ ] Re-read the official rules. Do not rely on any summary of them, including this one.
- [ ] Confirm every service claimed in the video is actually running — `/api/v1/system`
      reports the real configuration.
- [ ] Re-run `pytest` and the benchmark on a clean checkout.
