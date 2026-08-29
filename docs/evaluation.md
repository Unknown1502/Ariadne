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

Expected verdict spread: 3 SUPPORTED, 4 CONTRADICTED, 5 INCONCLUSIVE, 2 NO_VERDICT. A test
asserts no single answer exceeds 60% of the suite, because an all-CONTRADICTED benchmark
would make an always-contradict system look perfect.

## Results

| Configuration | Accuracy | False support | False contradiction | Inconclusive calibration |
|---|---|---|---|---|
| **full** | **100%** (14/14) | 0 (0%) | 0 (0%) | 100% |
| no control arm | 93% (13/14) | 1 (8%) | 0 (0%) | 100% |
| no validity gate | 79% (11/14) | 0 (0%) | 3 (25%) | 50% |
| trust the model's explanation | 29% (4/14) | 10 (83%) | 0 (0%) | 0% |

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

**Is any of this better than believing the model?** `self-report` reads the target model's own
explanation and concludes SUPPORTED because that is what it asserts. 83% false-support rate.
It is a fixed rule, not a language model, and the report says so.

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
- Claim compilation runs on the offline deterministic reasoner by default, so these numbers
  do **not** measure Gemini's claim-extraction quality. Evaluating that needs a labelled set
  of real explanations and is genuine future work.
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

939 tests, 92% line coverage on `backend/`. Every test is hermetic: no network, no cloud
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
