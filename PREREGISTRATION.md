# Pre-registration and threshold provenance

## The admission this document exists to make

**Ariadne's thresholds were not pre-registered.** The repository was built in a single pass,
so there is no commit history that could establish the 0.10 minimum effect, 0.80
reproducibility, 0.90 validity, or 15% minimum perturbation were fixed before any benchmark
result was seen. They probably were — but "probably were, take my word for it" is not
evidence, and a reviewer is right to treat an unfalsifiable claim of good practice as no
claim at all.

Stating that plainly costs less than being caught. What follows is the evidence offered
instead, and the commitments that bind everything from here.

---

## 1. What each threshold is, and where the number came from

| Parameter | Value | Provenance |
|---|---|---|
| `min_effect_threshold` | 0.10 | Laboratory design — see §2 |
| `reproducibility_threshold` | 0.80 | Convention. **The benchmark cannot justify it** — see §4 |
| `validity_threshold` | 0.90 | Convention, chosen strict; a probe that fails 10% of its own constraints is not a probe |
| minimum perturbation | 15% of feature range | Convention. Below it, "neutralize" is not a real intervention |
| `MIN_TESTABILITY` | 0.30 | Convention |
| `CONTROL_DOMINANCE_FRACTION` | 0.50 | Chosen as a margin rather than a bare inequality, so a verdict cannot flip on a difference too small to measure |

Only the first has a derivation. The rest are conventions, and calling them conventions is
more useful than dressing them up.

## 2. The one threshold with a derivation

The effect threshold has to sit above the largest effect a *false* explanation produces and
below the smallest a *true* one produces. In the synthetic laboratory both are analytic,
because the coefficients are published:

```
                effect of neutralizing urgency      sd        explanation is
v1.0.0                    -0.0549               0.0171       false
v4.0.0                    -0.0574               0.0199       false (interaction only)
v3.0.0                    -0.1335               0.1463       genuinely ambiguous
v2.0.0                    -0.2195               0.0683       true
```

0.10 sits ~1.8× above the largest false-explanation effect and ~2.2× below the true one. It
is the midpoint of a gap the laboratory's coefficients created, which is a design choice
about the laboratory, not a result read off the benchmark.

**For a real model the derivation is different and stronger.** The threshold must exceed the
model's own noise floor, or the experiment measures sampling variance. From the one real
measurement in this repository — Gemini 2.5 Flash, sd = 0.0255 — the replicate count needed
for an effect to clear 4 standard errors is:

```
threshold 0.05  ->  5 replicates  (~370 calls per investigation)
threshold 0.10  ->  2 replicates  (~148 calls per investigation)
threshold 0.15  ->  1 replicate   (~74  calls per investigation)
```

That is the honest reason 0.10 is defensible on a real model: it is the loosest threshold
that still forces replication, at a cost that does not make auditing prohibitive. Gemini's
*maximum* observed spread on identical input was 0.165 — larger than the threshold itself —
which is why replication is mandatory rather than advisory.

## 3. Sensitivity, offered in place of pre-registration

`python -m benchmark.sensitivity` — reproducible, and reported whichever way it lands.

| effect threshold | uniform | as designed |
|---|---|---|
| 0.04 | 12/14 | 13/14 |
| 0.06 | 12/14 | 13/14 |
| **0.08 – 0.12** | **13/14** | **14/14** |
| 0.15 | 12/14 | 13/14 |
| 0.20 | 12/14 | 13/14 |
| 0.25 | 12/14 | 13/14 |

**The result sits on a plateau spanning ±20% around the default, not on a knife edge.** That
is the substantive answer to "you tuned this": a tuned parameter is one where the result
collapses as soon as you move it, and this one does not.

*uniform* forces every case onto a single threshold, stripping the per-case overrides.
*as designed* keeps them. The gap is one case — `primacy-refuted-by-control`, which
**requires** a lowered threshold to exist at all: its whole purpose is to construct the
condition where v1's urgency effect *is* reproducible, so that the control arm becomes the
only thing that can refute the claim. Stripping that override does not remove a tuning, it
destroys the experiment. Both columns are published so a reader can decide that for
themselves.

## 4. A threshold this benchmark cannot justify

| reproducibility threshold | 0.60 | 0.70 | 0.75 | **0.80** | 0.85 | 0.90 | 1.00 |
|---|---|---|---|---|---|---|---|
| correct | 14/14 | 14/14 | 14/14 | **14/14** | 14/14 | 14/14 | 14/14 |

**Completely flat.** No case in this suite sits near the reproducibility boundary, so the
benchmark provides no evidence for 0.80 over any other value in that range. The number is a
convention and this benchmark cannot promote it to a finding.

Naming this is the point. A sensitivity analysis that only reports the parameter that looks
good is advocacy.

## 5. Binding commitments

These bind the expanded benchmark and every result reported from it.

1. **Thresholds are frozen at the values in §1** before any expanded case is generated.
   Changing one requires a new `PROTOCOL_VERSION`, which appears on every verdict, so a
   result computed under different thresholds is not silently comparable with one that is not.
2. **A held-out set is drawn before any result is inspected**, by deterministic hash of the
   case id, and its membership is committed. No threshold, rule, or case may be changed in
   response to held-out performance. If one is, the held-out set is burned and a fresh one
   drawn — and that fact is reported.
3. **Every rate is reported with a 95% interval.** A point estimate at n=14 is not a result.
4. **Paired comparisons use McNemar**, not chi-square, because the configurations run over
   identical cases.
5. **Multiple comparisons are corrected** (Holm–Bonferroni) across ablation configurations.
6. **Primary metric: false-support rate.** Declared here, before the expanded run, because a
   primary metric chosen afterwards is chosen to win. Justification: a false SUPPORTED sends
   a nurse false assurance, a false CONTRADICTED sends an engineer on a goose chase, and the
   harms are not symmetric.
7. **Negative and null results are reported.** If the expanded benchmark fails to separate
   `full` from `no-control`, that is the finding.

## 6. What would falsify the threshold choice

- An expanded benchmark where accuracy is *not* flat across 0.08–0.12 — the plateau in §3
  would then be an artifact of 14 cases rather than a property of the protocol.
- A real model whose noise floor exceeds 0.10, making the threshold unreachable without
  replication counts that cost more than the audit is worth. Gemini's *maximum* spread
  already exceeds it; its *standard deviation* does not. A model where the sd exceeds it
  would need a different threshold, and the protocol version would have to say so.
