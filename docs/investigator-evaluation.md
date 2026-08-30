# Measuring the Investigator

The gap this closes was stated in `docs/limitations.md` for the whole build and never acted
on: **every benchmark number in this repository was produced with claim extraction done by a
keyword matcher.** `OfflineReasoner` searches for substrings and reads testability off a
three-way lookup. So "Ariadne verifies explanations" had never been tested end to end. What
had been tested was that the verifier correctly processes claims a substring search produced.

This is the measurement. Reproduce it:

```bash
python -m benchmark.investigator_eval                                # offline, free
python -m benchmark.investigator_eval --gemini --project <project>   # live, ~25 calls
```

## The instrument

25 explanations with ground truth recorded per case — what a careful annotator would say the
sentence claims, plus **why**, so a reader can disagree with a specific judgement instead of
the whole set. One rule makes the corpus fair:

> **No case is phrased using the extractor's own vocabulary.**

Writing *"urgency_marker was the primary driver"* and then scoring a matcher that searches for
`"primary"` and `"urgency_marker"` measures nothing but the author's ability to copy a word
list. Every case is phrased the way a person actually writes. A test enforces this against
`PRIMACY_WORDS` and `VAGUE_WORDS` directly, so the corpus cannot quietly drift toward being
easy.

| Stratum | n | What it isolates |
|---|---|---|
| `faithful-primacy` | 5 | primacy asserted with no keyword — *"nothing else came close"* |
| `faithful-influence` | 5 | contribution asserted, primacy explicitly denied |
| `attribution-trap` | 4 | one feature named, a **different** one asserted as the driver |
| `negation` | 2 | the named feature denied as the driver |
| `paraphrase` | 4 | one claim, four natural rewordings |
| `vague` | 3 | no single testable driver, no keyword |
| `multi-causal` | 2 | two drivers, no ranking between them |

`faithful-primacy` against `faithful-influence` is the discriminating pair. Identical
measurements produce opposite verdicts depending on whether primacy was claimed, so an
extractor that cannot separate *"the main reason"* from *"one of the reasons"* makes the
protocol's claim-sensitivity a fiction no matter how well it finds feature names.

## Results

| | offline keyword matcher | Gemini 2.5 Flash (live) |
|---|---|---|
| subject accuracy | 76.0% | 80.0% |
| **primacy F1** | **0.143** | **0.963** |
| testability accuracy | 84.0% | 88.0% |
| fully correct | 44.0% | 80.0% |
| **consequential error rate** | **48.0%** | **12.0%** |

Per stratum, fully correct:

| Stratum | offline | Gemini |
|---|---|---|
| faithful-primacy | **0%** | **100%** |
| faithful-influence | 100% | 100% |
| attribution-trap | 25% | **100%** |
| negation | 100% | 100% |
| paraphrase | **0%** | **100%** |
| vague | 100% | 0% |
| multi-causal | 0% | 0% |

### The metric that matters

**Consequential error rate** is a wrong extraction the testability gate does *not* absorb.
An extractor that names a driver for *"the model weighed everything it saw"* but scores it
0.1 testable is wrong on paper and harmless in practice — the claim never reaches an
experiment and the verdict is INCONCLUSIVE either way. One that scores the same sentence 0.6
has manufactured a hypothesis out of a non-statement, and the system will go and test it.
Only the second kind can produce a wrong verdict.

Both extractors have exactly 2 errors absorbed by the gate. The difference is what gets
through: **12 consequential errors offline, 3 with Gemini.**

### Error propagation — the decisive analysis

Each extracted claim and each annotator-correct claim was run through the real engine and
verifier, and the verdicts compared.

```
offline    P(verdict changed | extraction wrong) = 0.33
           P(verdict changed | extraction right) = 0.00

Gemini     no wrong extraction survived to a verdict, so the conditional is undefined
           P(verdict changed | extraction right) = 0.00
```

**Extraction errors are not absorbed by the protocol.** One wrong claim in three changes the
final verdict. This settles a question the architecture had only ever asserted an answer to:
the Investigator is a load-bearing component, not a formality, and the cost of getting it
wrong is a wrong verdict rather than a caught one.

## What this establishes, and what it does not

**Established: the language model is doing real work.** This is the empirical argument the
project previously made only by design intuition. A keyword matcher scores 0.143 on primacy
F1 — it detects primacy in **zero** of five cases where primacy is asserted without one of
its keywords, and **zero** of four paraphrases. Gemini scores 0.963, and is perfect on all
five strata that name a single driver, including the attribution traps where the sentence
contains a primacy word attached to the *wrong* feature.

**Established: Gemini's failure mode is over-confidence, not misreading.** Its only
consequential errors are the three cases naming no single testable driver, where it
manufactures one rather than declining:

| case | text | Gemini's testability |
|---|---|---|
| `vague-03` | *"This case simply looked high risk."* | 0.4 — above the gate |
| `multi-01` | *"Urgency and signal_c together account for this score."* | 0.6 |
| `multi-02` | *"Both the urgency reading and signal_b contributed roughly equally."* | 0.6 |

A conjunction is not testable by a single-variable neutralization, and `multi-02` explicitly
says neither driver is primary. Silently picking one converts a statement the protocol cannot
test into one it will happily test wrongly. **This is a real, specific, open defect**, and the
fix belongs in the claim compiler's prompt rather than anywhere downstream.

**Not established: that these numbers are stable.** Two consecutive live runs over the same
25 cases gave primacy F1 of 1.000 and 0.963 — one case flipped. That is Gemini's measured
non-determinism at `temperature=0`, the same property `docs/real-model-audit.md` reports
(spread up to 0.165), appearing inside this project's own evaluation of itself. Single-run
figures here carry run-to-run variance and should be read as approximate. Replicating this
corpus properly would need the same `replicates_needed` treatment the target-model audit gets.

**Not established: anything about other models, or about real explanations.** One model, one
prompt, 25 hand-written explanations over a synthetic feature space. The corpus is written by
the same person who wrote the extractor being criticised, which is a real conflict of
interest — the mitigations are that the ground truth is published per case with reasoning, and
that a test forbids borrowing the extractor's vocabulary. Independent annotation would be
better and is not done here.

## A bug this found

The first live run failed on **every single case** with `MAX_TOKENS`. Gemini 2.5 spends
output tokens on internal reasoning before answering, and the agent-side `GeminiClient` never
set `thinking_config` — so thinking consumed the entire 2048-token budget before any JSON
appeared. The target-model adapter had set `thinking_budget: 0` since it was written; the two
clients had drifted, one carrying the fix and the other carrying the bug the fix was written
for.

Nothing caught it because the agent path had never run against a live model — exactly the gap
this document exists to close. It is also a second live vindication of the `finish_reason`
check from `docs/architecture-review.md` §F9: the failure named its own cause and pointed at
`GEMINI_MAX_OUTPUT_TOKENS` instead of surfacing as "malformed JSON" and sending someone to
debug a prompt that was fine.
