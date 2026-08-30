"""SUPPORTED must survive uncertainty, not just a head-count.

Ariadne computes a seeded 2000-iteration percentile bootstrap interval for the mean paired
delta (`statistics.bootstrap_ci`), stores it on `Evidence.effect_ci`, publishes it in the
verdict — and then never consults it. Every branch in `verify()` was a threshold comparison,
so "statistical verification" meant counting how many cases cleared a bar.

Counting is not robust to heterogeneous effects. This is the gap, and it is not exotic:

    nine cases move -0.20, one case moves +0.60

    reproducibility  0.9   clears the 0.80 bar
    mean effect     -0.12   clears the 0.10 bar, points the claimed way
    bootstrap CI    [-0.20, +0.04]   contains zero

Nine tenths of the evidence agrees with the claim and the tenth disagrees hard enough that
the average effect cannot be distinguished from no effect at all. The honest answer is
INCONCLUSIVE: the experiment did not settle the question. The counting rule said SUPPORTED.

False support is this project's primary metric because a false SUPPORTED sends a nurse a
false assurance. A gate that can only turn SUPPORTED into INCONCLUSIVE can only ever reduce
it, which is why this is the direction the gate cuts.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.core.enums import RunKind, VerdictStatus
from backend.core.schemas import Evidence, RunSummary
from backend.core.versions import PROTOCOL_VERSION
from backend.verifier.statistics import bootstrap_ci, effect_size, paired_deltas
from backend.verifier.verifier import ReasonCode, verify
from tests.factories import make_case

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def summary(kind: RunKind, scores: list[float]) -> RunSummary:
    return RunSummary(
        kind=kind, n=len(scores), mean=sum(scores) / len(scores), stdev=0.0,
        minimum=min(scores), maximum=max(scores), scores=scores,
        run_ids=[f"RUN-{kind}-{i}" for i in range(len(scores))],
    )


def evidence_from(
    claim, plan, baseline: list[float], intervention: list[float],
    control: list[float] | None = None, *, seed: int = 20260101,
) -> Evidence:
    """Build Evidence the way the runner does, including the bootstrap interval."""
    deltas = paired_deltas(baseline, intervention)
    interval = bootstrap_ci(deltas, seed=seed)
    return Evidence(
        id="EV-ci-gate", experiment_id=plan.id, claim_id=claim.id,
        claim_family_id=claim.claim_family_id, scope=claim.scope,
        protocol_version=PROTOCOL_VERSION,
        baseline=summary(RunKind.BASELINE, baseline),
        intervention=summary(RunKind.INTERVENTION, intervention),
        control=summary(RunKind.CONTROL, control) if control else None,
        effect_size=effect_size(deltas),
        effect_ci=(interval.low, interval.high) if interval else None,
        control_effect_size=(
            effect_size(paired_deltas(baseline, control)) if control else None
        ),
        reproducibility=0.0,  # recomputed by the verifier from the stored runs
        validity_score=1.0, instability=0.0,
        run_ids=["RUN-1"], input_hashes=["sha256:in"], output_hashes=["sha256:out"],
        evidence_hash="sha256:ev", created_at=T0,
    )


class TestSupportRequiresSeparationFromZero:
    def test_a_heterogeneous_effect_is_inconclusive_not_supported(self) -> None:
        """The case the counting rule gets wrong."""
        claim, plan = make_case("2.0.0")
        baseline = [0.40] * 9 + [0.30]
        intervention = [0.20] * 9 + [0.90]  # deltas: nine -0.20, one +0.60

        evidence = evidence_from(claim, plan, baseline, intervention)
        # Preconditions: this case *passes* every pre-existing rule.
        assert evidence.effect_size == pytest.approx(-0.12)
        assert evidence.effect_ci is not None
        assert evidence.effect_ci[0] <= 0.0 <= evidence.effect_ci[1]

        outcome = verify(evidence, claim, plan)
        assert outcome.status is VerdictStatus.INCONCLUSIVE
        assert ReasonCode.EFFECT_NOT_SEPARATED_FROM_ZERO in outcome.reason_codes

    def test_a_clean_effect_is_still_supported(self) -> None:
        """The gate must not cost a genuine result: a tight interval clear of zero passes."""
        claim, plan = make_case("2.0.0")
        baseline = [0.60] * 10
        intervention = [0.38, 0.40, 0.39, 0.41, 0.40, 0.38, 0.42, 0.39, 0.40, 0.41]

        evidence = evidence_from(claim, plan, baseline, intervention)
        assert evidence.effect_ci is not None
        assert evidence.effect_ci[1] < 0.0, "a clean decrease should not straddle zero"

        outcome = verify(evidence, claim, plan)
        assert outcome.status is VerdictStatus.SUPPORTED
        assert ReasonCode.EFFECT_NOT_SEPARATED_FROM_ZERO not in outcome.reason_codes

    def test_the_gate_is_skipped_when_no_interval_exists(self) -> None:
        """Below four observations a bootstrap interval is arithmetic theatre.

        `bootstrap_ci` returns None there rather than inventing precision, so the gate has
        nothing to consult and must not invent a refusal either.
        """
        claim, plan = make_case("2.0.0", repetitions=3, min_repetitions_for_verdict=3)
        evidence = evidence_from(claim, plan, [0.60, 0.60, 0.60], [0.30, 0.31, 0.29])

        assert evidence.effect_ci is None
        outcome = verify(evidence, claim, plan)
        assert outcome.status is VerdictStatus.SUPPORTED
