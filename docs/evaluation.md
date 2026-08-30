# Evaluation

```bash
python -m benchmark.run_benchmark --out var/benchmark --fail-on-regression
```

Writes `benchmark.json` and `benchmark.md`. Runs in CI as `tests/benchmark/`, because a
silent drop from 14/14 to 13/14 would otherwise reach a judge before it reached us.

## Ground truth

Derived from the published model formulas and fixed fixture seeds. **No language model is
consulted about what the right answer is** — that is the only reason it is legitimate to
score anything against these values.

```
v1.0.0   score = 0.2*urgency_marker + 0.05*signal_b + 0.75*signal_c
v2.0.0   score = 0.8*urgency_marker + 0.05*signal_b + 0.15*signal_c
v3.0.0   score = 0.5*urgency_marker + 0.05*signal_b + 0.45*signal_c + N(0,0.1) keyed by input
v4.0.0   score = 0.1*urgency_marker + 0.7*signal_c + 0.15*urgency_marker*signal_c
```

Every case records *why* its expected verdict is correct in terms a reader can check by
hand. If a rationale does not survive scrutiny, the case is wrong — not the system.

Expected verdict spread: **2 SUPPORTED, 4 CONTRADICTED, 6 INCONCLUSIVE, 2 NO_VERDICT.** A
test asserts no single answer exceeds 60% of the suite, because an all-CONTRADICTED benchmark
would make an always-contradict system look perfect.

This distribution is not decoration. It fully determines the `assume-faithful` row below, and
the report prints it beside that row for exactly that reason.

## Results

| Configuration | Accuracy | 95% CI (Wilson) | False support | False contradiction | Inconclusive calibration |
|---|---|---|---|---|---|
| **full** | **100%** (14/14) | [78.5%, 100%] | 0 (0%) | 0 (0%) | 100% |
| no control arm | 93% (13/14) | [68.5%, 98.7%] | 1 (8%) | 0 (0%) | 100% |
| no validity gate | 79% (11/14) | [52.4%, 92.4%] | 0 (0%) | 3 (25%) | 50% |
| *assume-faithful (floor)* | 29% (4/14) | [11.7%, 54.6%] | 10 (83%) | 0 (0%) | 0% |

### What n=14 can and cannot support

The intervals matter more than the point estimates, and they are wide.

- **full vs no-control is not a distinguishable difference.** They differ by one case.
  McNemar on a single discordant pair gives **p = 1.0** — the largest value the test can
  return. The control arm may well earn its place; *this benchmark cannot show that it does.*
  The mechanism argument in "Why ablations" below is a design argument, not evidence.
- **full vs no-validity is the comparison that survives.** Three false contradictions
  against zero, and the failure mode is interpretable: every one is a distribution-shift case
  where the probe cannot move the input enough to test anything.
- **Any accuracy at n=14 carries a ±22pp interval.** Reporting "100%" without that interval
  would overstate what fourteen hand-written cases can establish.

n=14 is a laboratory, not an evaluation. Its job is to make the verifier's own correctness
checkable by hand; drawing population conclusions from it would be a category error, and the
remedy is more cases rather than more confident phrasing.

Reliability scenarios, scored pass/fail on behaviour: duplicate delivery, worker crash
mid-experiment, malformed agent output, dead target model. All pass.

## Why ablations instead of baselines

The design pack suggests comparing against "a fixed test suite" and "single-agent
orchestration". Scoring those honestly would mean building them; simulating one with a stub
that always answers SUPPORTED would produce numbers describing the stub, not anything real.

Ablations avoid that: identical code, identical fixtures, one mechanism removed. Each column
answers a question a reviewer should ask.

**Does the control arm earn its place?** One case isolates it — `primacy-refuted-by-control`,
where at a lowered threshold v1's urgency effect *is* reproducible, so the effect-absence
rule no longer applies. The claim still fails, because signal_c moves the score three times
as much and the explanation asserted primacy. Remove the control and the same evidence reads
as SUPPORTED. Its twin, `influence-claim-survives-control`, has identical numbers against a
claim that never asserted primacy, and correctly comes out SUPPORTED. Together they show the
verdict tracks *what was claimed*, not just what was measured.

**Does the validity gate prevent anything?** Three false contradictions without it. All three
are distribution-shift cases where the probe cannot move the input enough to test anything —
exactly the situation where a naive system manufactures a refutation.

**Is any of this better than "always say yes"?** `assume-faithful` is the constant
`SUPPORTED`. It does not read the explanation, it is not a language model, and it is not a
simulation of one.

Its 83% false-support rate is **an arithmetic identity, not a measurement**: of the 12 cases
that reach a verdict, 10 are declared non-SUPPORTED by the benchmark, and 10/12 = 83%.
Writing a benchmark with a different verdict mix would move that number without changing
anything about explanations or about Ariadne. Quoting it as "trusting a model costs you 83%"
would be circular, and it is not quoted that way anywhere in this repository — a test
(`test_the_floor_reference_matches_the_benchmark_mix_it_restates`) pins it to the computation
so the two cannot drift apart.

What it is legitimately for: a floor. Any configuration that cannot beat "always say yes" is
not doing useful work. It bounds the bottom of the scale and says nothing about the top.

**The honest statement of what is still missing:** measuring what trusting a *real* model
costs requires real explanations from real models with known ground truth. That experiment is
not in this benchmark, and no number here is a substitute for it.

## Metrics

Verdict accuracy, false-support rate, false-contradiction rate, inconclusive calibration,
mean intervention validity, mean reproducibility, mean latency, and no-verdict correctness —
per configuration, in the JSON report.

**False support is the dangerous error.** Blessing an explanation that should not be blessed
sends a nurse a false assurance; a false contradiction sends an engineer on a wild goose
chase. Both are tracked separately and neither is folded into a single accuracy number.

**Inconclusive calibration** is the fraction of cases that *should* be inconclusive and are.
Reported separately because a system can trivially reach zero false verdicts by answering
INCONCLUSIVE to everything, and this is the number that catches it.

## What these numbers do not mean

- The target model is four hand-written formulas over invented features. Nothing here says
  anything about any real model's explanations.
- Accuracy measures the verifier against a laboratory whose ground truth we defined. It is a
  measure of internal correctness, not of external validity.
- The ablations vary one mechanism within Ariadne. They are not a comparison against any
  other published system, and no such comparison is claimed.
- Claim compilation runs on the offline deterministic reasoner, so these numbers do **not**
  measure Gemini's claim-extraction quality. That is measured separately in
  `docs/investigator-evaluation.md`, and the result matters for reading this table: the
  matcher producing the claims scored here has a primacy F1 of **0.143**, and extraction
  errors change the final verdict a third of the time. These accuracies are therefore the
  verifier's, given claims from a weak extractor — not the pipeline's.
- Debt figures are not comparable across policy versions, by construction.

## Test suite

```
tests/unit/          contracts, state machine, hashing, statistics, target models, agents,
                     remote adapters, the Gemini target codec, structured logging
tests/integration/   verifier ground truth, engine, lineage, chain integrity, debt,
                     governor, runtime, API, the demo script's narrative
tests/chaos/         failure injection: dead models, malformed output, crashes, storage faults
tests/security/      injection payloads, privilege boundaries, ledger tampering
tests/benchmark/     the benchmark itself, as a regression gate
```

1203 tests, 92% line coverage on `backend/`. Every test is hermetic: no network, no cloud
account, no wall-clock dependency, no unseeded randomness. A failure means a regression, not a
flake. The 24 skips are the Firestore and Pub/Sub emulator suites, which need Docker and skip
cleanly without it.

### Four suites that exist because prose is not verification

This project keeps finding the same defect species in itself — something described
confidently that no test holds to its description. Four suites now automate the check rather
than relying on anyone remembering to make it:

| Suite | What it refuses to let drift |
|---|---|
| `test_config_is_honest.py` | a setting that is accepted, reported, and read by nothing |
| `test_readme_is_current.py` | a README claim a machine can check — test counts, formulas, ablation figures, every doc link |
| `test_docs_are_current.py` | every repository path the other docs name, and every mermaid block being well-formed |
| `test_demo_narrative.py` | the demo script's *output*, which CI previously checked only for exit status |

The last is the newest and found the most: asserting what `run_demo.py` prints surfaced a
forked hash chain in the append-only ledger. See `docs/architecture-review.md` §F10.

The lesson each of these encodes is the one in `docs/decisions.md`: *a check that has to be
remembered will be forgotten.* Every one of them was written after the thing it guards had
already gone wrong at least once.
