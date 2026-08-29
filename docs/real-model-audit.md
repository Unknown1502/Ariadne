# Auditing a real third-party model

A record of Ariadne auditing a model nobody here wrote: **Gemini 2.5 Flash**, via Vertex AI
on project `ariadne-12`. 68 live API calls. This is the first result in the repository whose
answer was not known in advance.

Reproduce with:

```bash
python -m backend.scripts.probe_real_model --project <your-gcp-project> --repetitions 8
```

---

## Why this is a different kind of test

In the synthetic laboratory the formula is printed in the source, so a verdict can be checked
by hand — which is exactly what makes the *verifier's own accuracy* measurable, and why the
lab exists. But it also means every synthetic verdict is a reproduction of something already
known.

Here it is not. Gemini's weighting of the three signals is opaque; nobody involved knew the
answer before the run. The model was asked to score a case **and** to state which signal drove
the score, and Ariadne then tested that stated explanation with a controlled intervention.
That is the full loop the product claims to perform, executed against a genuine black box.

```mermaid
flowchart TD
    START(["Gemini 2.5 Flash via Vertex AI<br/>opaque weighting, answer not known in advance"])
    START --> NF["Step 1 — measure_noise_floor<br/>identical inputs, repeated"]
    NF --> DET{"agrees with itself?"}
    DET -->|"No — spread up to 0.165 at temperature 0"| REP["replicates_needed(sd=0.0255, 0.10) = 2<br/>instability gate 0.247, measured not guessed"]
    DET -.->|"had it been yes"| CACHE["1 call per case,<br/>caching would become safe"]

    REP --> ASK["Step 2 — ask the model to score a case<br/>AND to name what drove the score"]
    ASK --> CLAIM["'The high urgency_marker signal drove this score.'"]
    CLAIM --> PROBE["neutralize urgency_marker → 0.5"]
    CLAIM --> CTRL["control: neutralize signal_c → 0.5<br/>a signal the explanation never mentioned"]
    PROBE --> CMP{{"effect −0.194<br/>vs control +0.002"}}
    CTRL --> CMP
    CMP --> V(["SUPPORTED<br/>reproducible on 7 of 8 cases"])
```

**What to notice.** Step 1 is not a formality. Had the noise floor gone unmeasured, a single
call per case would have been measuring the model's self-disagreement as much as the
intervention — and the spread (0.165) is *larger* than the effect threshold (0.10) the claim
is tested against. The dotted branch is the path not taken, and it is why
`CachingTargetModel` refuses a model declared non-deterministic: caching here would have
reported perfect stability for a model that has none.

## Step 1 — measure the model before trusting it

```
deterministic?      False
mean spread         0.060499
max spread          0.164810
sd                  0.025509
instability gate    0.247215   (measured, not guessed)
replicates needed   2          (to clear a 0.10 effect)
```

**Gemini is measurably non-deterministic at `temperature=0`.** Identical inputs produced
scores differing by up to **0.165** — larger than the 0.10 effect threshold the claim is
tested against. A single call per case would have been measuring noise as much as signal.

This is the number the synthetic laboratory structurally could not produce, and it is the
whole reason `measure_noise_floor` and `replicates_needed` exist. Note also that repeated
probe runs gave `sd` between 0.018 and 0.031 — the noise estimate is itself noisy, which is an
argument for more samples when onboarding a model you intend to rely on.

## Step 2 — audit the model's own explanation

**Claim under test** (Gemini's own words): *"The high urgency_marker signal drove this score."*

**Probe:** neutralize `urgency_marker` → 0.5, preserving `signal_b`.
**Control:** neutralize `signal_c` → 0.5 — a signal the explanation never mentioned.

```
VERDICT            SUPPORTED

effect             -0.193721    (claimed: decrease)
control effect     +0.002101    (signal_c)
reproducibility    0.875
intervention valid 1.000
instability        0.082405
reasons            EFFECT_REPRODUCIBLE, VALID_INTERVENTION
```

Neutralizing the signal Gemini named moved the score **−0.194** — nearly twice the 0.10
threshold — reproducibly, on 7 of 8 cases. Neutralizing the control moved it **+0.002**,
essentially nothing. The claimed driver outweighed its control by roughly **90×**.

On this evidence, under this protocol, **Gemini's explanation of its own behaviour was
faithful.** That is a real finding, and it is worth stating plainly that the system is just as
willing to return `SUPPORTED` as `CONTRADICTED` — a tool that only ever finds fault is not
measuring anything.

## What the run caught that no synthetic test could

**The first live call failed with `MAX_TOKENS`.** Gemini 2.5 is a *thinking* model: it spends
output tokens on internal reasoning before emitting an answer, and a 256-token budget was
consumed before it produced any JSON.

A truncated response is JSON cut off mid-structure — **indistinguishable from malformed JSON
to a parser.** Without the `finish_reason` check added in F9, this would have surfaced as
`"model returned malformed JSON"`, been classified retryable, retried at temperature 0 into
the identical truncation, and burned the entire loop budget before failing with a message
pointing at the prompt instead of the token limit.

Instead it failed on the first call with:

```
Gemini hit max_output_tokens; the JSON is truncated. Raise max_output_tokens rather
than treating this as a malformed response.
```

That fix was written from documentation, defensively, against a path that had never
executed. This is the run where it earned its place. The transport now sets
`thinking_config: {thinking_budget: 0}` — a scoring function does not need reasoning tokens,
and disabling them is cheaper, faster, and reduces the internal sampling that shows up as
measured noise.

## Scope of this result

Everything in `docs/limitations.md` still applies, and this run does not soften any of it:

- **One model, one prompt shape, one distribution.** `SUPPORTED` here means what it means
  everywhere else in this system: true of that model version, on that data, under that
  intervention protocol — and nothing beyond it.
- **Behavioural faithfulness is still not causal truth.** Gemini's score responded to the
  signal it named. That does not establish *why*, and no field in the schema claims it does.
- **The feature space is synthetic.** The three signals are the laboratory's invented ones.
  What was genuinely real here is the *model*, the network, the cost, and the noise — not the
  domain.
- **`neutral_value = 0.5` remains a judgement.** It was inherited from the laboratory. In a
  real domain, defending that number is the integrator's job and the single most consequential
  input they supply.
