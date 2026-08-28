"""Deterministic statistics.

Every function here is a pure function of its inputs plus an explicit seed. There is no
global RNG, no wall-clock, and no dependency on a numerical library whose version could
change a published result. The confidence interval is a seeded bootstrap rather than a
closed-form normal approximation, because the paired deltas are not normal for the noisy
model version and a normal CI there would be quietly wrong.

Rounding is applied at the boundary only. Intermediate values keep full precision so the
verifier does not decide a verdict on a rounding artifact.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from backend.core.enums import ExpectedDirection, RunKind
from backend.core.errors import ValidationError
from backend.core.schemas import ExperimentRun, RunSummary

BOOTSTRAP_ITERATIONS: int = 2000
PRECISION: int = 9


def summarize(kind: RunKind, runs: list[ExperimentRun]) -> RunSummary:
    """Aggregate one arm of an experiment into its persisted summary."""
    scores = [run.score for run in runs]
    if not scores:
        return RunSummary(kind=kind, n=0, mean=0.0, stdev=0.0, minimum=0.0, maximum=0.0)
    return RunSummary(
        kind=kind,
        n=len(scores),
        mean=round(mean(scores), PRECISION),
        stdev=round(stdev(scores), PRECISION),
        minimum=round(min(scores), PRECISION),
        maximum=round(max(scores), PRECISION),
        scores=[round(s, PRECISION) for s in scores],
        run_ids=[run.id for run in runs],
    )


def mean(values: list[float]) -> float:
    if not values:
        raise ValidationError("mean of an empty sample is undefined")
    return math.fsum(values) / len(values)


def stdev(values: list[float]) -> float:
    """Population standard deviation.

    Population rather than sample: these are all the runs that were executed, not a sample
    drawn from a larger set of runs, so there is no degree of freedom to give back.
    """
    if len(values) < 2:
        return 0.0
    mu = mean(values)
    return math.sqrt(math.fsum((v - mu) ** 2 for v in values) / len(values))


def paired_deltas(baseline: list[float], intervention: list[float]) -> list[float]:
    """Per-case (intervention - baseline).

    Paired on purpose. Both arms run the same fixture cases in the same order, so pairing
    removes between-case variance entirely and the remaining signal is attributable to the
    intervention rather than to which cases happened to land in which arm.
    """
    if len(baseline) != len(intervention):
        raise ValidationError(
            f"paired comparison needs equal arms, got {len(baseline)} and {len(intervention)}"
        )
    return [i - b for b, i in zip(baseline, intervention, strict=True)]


def effect_size(deltas: list[float]) -> float:
    """Mean paired change, in the target model's own score units."""
    return round(mean(deltas), PRECISION) if deltas else 0.0


def standardized_effect(deltas: list[float]) -> float:
    """Cohen's d for paired samples. Reported alongside the raw effect, never instead of it.

    A standardized effect is unitless and comparable across models; the raw effect is what
    the operational threshold is expressed in. Both are recorded so neither has to be
    reverse-engineered later.
    """
    if len(deltas) < 2:
        return 0.0
    spread = stdev(deltas)
    if spread == 0.0:
        return 0.0 if effect_size(deltas) == 0.0 else math.copysign(float("inf"), mean(deltas))
    return round(mean(deltas) / spread, PRECISION)


def direction_of(value: float, threshold: float) -> ExpectedDirection:
    """Classify an observed change against the minimum meaningful effect."""
    if value <= -abs(threshold):
        return ExpectedDirection.DECREASE
    if value >= abs(threshold):
        return ExpectedDirection.INCREASE
    return ExpectedDirection.NO_CHANGE


def matches_expectation(
    delta: float, expected: ExpectedDirection, threshold: float
) -> bool:
    """Did one case behave the way the claim predicted?

    NO_CHANGE is the interesting case: it asserts stability, so it is satisfied by a delta
    *below* the threshold rather than above it.
    """
    magnitude = abs(delta)
    match expected:
        case ExpectedDirection.DECREASE:
            return delta <= -abs(threshold)
        case ExpectedDirection.INCREASE:
            return delta >= abs(threshold)
        case ExpectedDirection.CHANGE:
            return magnitude >= abs(threshold)
        case ExpectedDirection.NO_CHANGE:
            return magnitude < abs(threshold)
    raise ValidationError(f"unsupported expected direction {expected!r}")


def reproducibility(
    deltas: list[float], expected: ExpectedDirection, threshold: float
) -> float:
    """Fraction of cases that behaved as the claim predicted.

    This is the pivotal quantity. A high value means the predicted effect is reproducibly
    present; a value near zero means it is reproducibly *absent*, which is evidence against
    the claim rather than an absence of evidence. Values in between are what INCONCLUSIVE
    is for.
    """
    if not deltas:
        return 0.0
    matches = sum(1 for d in deltas if matches_expectation(d, expected, threshold))
    return round(matches / len(deltas), PRECISION)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    low: float
    high: float
    iterations: int
    seed: int


def bootstrap_ci(
    deltas: list[float],
    *,
    seed: int,
    confidence: float = 0.95,
    iterations: int = BOOTSTRAP_ITERATIONS,
) -> BootstrapResult | None:
    """Percentile bootstrap CI for the mean paired delta.

    Returns None below four observations, where a bootstrap interval would be arithmetic
    theatre rather than an estimate. Seeded, so the published interval is reproducible.
    """
    n = len(deltas)
    if n < 4:
        return None
    if not 0.0 < confidence < 1.0:
        raise ValidationError(f"confidence must lie in (0, 1), got {confidence}")

    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(iterations):
        resample = [deltas[rng.randrange(n)] for _ in range(n)]
        means.append(math.fsum(resample) / n)
    means.sort()

    tail = (1.0 - confidence) / 2.0
    low = means[max(0, int(math.floor(tail * iterations)))]
    high = means[min(iterations - 1, int(math.ceil((1.0 - tail) * iterations)) - 1)]
    return BootstrapResult(
        low=round(low, PRECISION), high=round(high, PRECISION), iterations=iterations, seed=seed
    )


def instability(replicates: list[tuple[float, float]]) -> float:
    """Largest disagreement between two runs of the *same* input.

    A target model that answers differently when asked the same question twice cannot be
    probed by a paired design: the deltas would mix the intervention's effect with the
    model's own jitter. The verifier refuses to issue a verdict when this is non-trivial.
    """
    if not replicates:
        return 0.0
    return round(max(abs(first - second) for first, second in replicates), PRECISION)
