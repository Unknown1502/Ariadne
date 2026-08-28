"""Statistics tests.

These functions decide verdicts, so their edge cases are the system's edge cases: empty
samples, zero variance, ties exactly on a threshold, and the NO_CHANGE direction whose
logic is inverted relative to every other one.
"""

from __future__ import annotations

import pytest

from backend.core.enums import ExpectedDirection, RunKind
from backend.core.errors import ValidationError
from backend.verifier.statistics import (
    bootstrap_ci,
    direction_of,
    effect_size,
    instability,
    matches_expectation,
    mean,
    paired_deltas,
    reproducibility,
    standardized_effect,
    stdev,
    summarize,
)


class TestBasics:
    def test_mean_of_an_empty_sample_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="undefined"):
            mean([])

    def test_stdev_of_a_single_value_is_zero(self) -> None:
        assert stdev([0.5]) == 0.0

    def test_stdev_of_identical_values_is_zero(self) -> None:
        assert stdev([0.5] * 10) == 0.0

    def test_mean_is_numerically_stable(self) -> None:
        # fsum rather than sum: naive accumulation drifts on long runs of small deltas.
        values = [0.1] * 1000
        assert mean(values) == pytest.approx(0.1, abs=1e-15)


class TestPairing:
    def test_deltas_are_intervention_minus_baseline(self) -> None:
        assert paired_deltas([0.7, 0.6], [0.5, 0.5]) == pytest.approx([-0.2, -0.1])

    def test_unequal_arms_are_refused(self) -> None:
        with pytest.raises(ValidationError, match="equal arms"):
            paired_deltas([0.7, 0.6], [0.5])

    def test_effect_size_of_no_deltas_is_zero(self) -> None:
        assert effect_size([]) == 0.0


class TestDirection:
    def test_direction_respects_the_threshold(self) -> None:
        assert direction_of(-0.2, 0.1) is ExpectedDirection.DECREASE
        assert direction_of(0.2, 0.1) is ExpectedDirection.INCREASE
        assert direction_of(-0.05, 0.1) is ExpectedDirection.NO_CHANGE

    def test_a_value_exactly_on_the_threshold_counts(self) -> None:
        assert direction_of(-0.1, 0.1) is ExpectedDirection.DECREASE
        assert matches_expectation(-0.1, ExpectedDirection.DECREASE, 0.1)

    def test_no_change_is_satisfied_by_a_small_delta(self) -> None:
        # Inverted relative to every other direction: NO_CHANGE asserts stability, so it
        # is met *below* the threshold.
        assert matches_expectation(0.01, ExpectedDirection.NO_CHANGE, 0.1)
        assert not matches_expectation(0.5, ExpectedDirection.NO_CHANGE, 0.1)

    def test_change_accepts_either_sign(self) -> None:
        assert matches_expectation(0.5, ExpectedDirection.CHANGE, 0.1)
        assert matches_expectation(-0.5, ExpectedDirection.CHANGE, 0.1)
        assert not matches_expectation(0.01, ExpectedDirection.CHANGE, 0.1)

    def test_a_decrease_expectation_rejects_an_increase(self) -> None:
        assert not matches_expectation(0.5, ExpectedDirection.DECREASE, 0.1)


class TestReproducibility:
    def test_all_matching_gives_one(self) -> None:
        deltas = [-0.2] * 10
        assert reproducibility(deltas, ExpectedDirection.DECREASE, 0.1) == 1.0

    def test_none_matching_gives_zero(self) -> None:
        deltas = [-0.01] * 10
        assert reproducibility(deltas, ExpectedDirection.DECREASE, 0.1) == 0.0

    def test_partial_matching_is_reported_honestly(self) -> None:
        deltas = [-0.2] * 6 + [-0.01] * 4
        assert reproducibility(deltas, ExpectedDirection.DECREASE, 0.1) == pytest.approx(0.6)

    def test_no_deltas_gives_zero(self) -> None:
        assert reproducibility([], ExpectedDirection.DECREASE, 0.1) == 0.0


class TestStandardizedEffect:
    def test_zero_variance_with_zero_effect_is_zero(self) -> None:
        assert standardized_effect([0.0] * 5) == 0.0

    def test_zero_variance_with_a_real_effect_is_unbounded(self) -> None:
        # A perfectly consistent non-zero effect has infinite standardized magnitude.
        # Reporting inf is honest; reporting a large finite number would be invented.
        assert standardized_effect([-0.2] * 5) == float("-inf")

    def test_a_single_delta_has_no_standardized_effect(self) -> None:
        assert standardized_effect([-0.2]) == 0.0

    def test_larger_spread_lowers_the_standardized_effect(self) -> None:
        tight = standardized_effect([-0.2, -0.21, -0.19, -0.2])
        loose = standardized_effect([-0.2, -0.6, 0.2, -0.4])
        assert abs(tight) > abs(loose)


class TestBootstrap:
    def test_too_few_observations_yields_no_interval(self) -> None:
        assert bootstrap_ci([-0.2, -0.1, -0.15], seed=1) is None

    def test_the_interval_is_seeded_and_reproducible(self) -> None:
        deltas = [-0.2, -0.1, -0.15, -0.25, -0.3, -0.05]
        first = bootstrap_ci(deltas, seed=42)
        second = bootstrap_ci(deltas, seed=42)
        assert first == second

    def test_a_different_seed_gives_a_different_but_similar_interval(self) -> None:
        deltas = [-0.2, -0.1, -0.15, -0.25, -0.3, -0.05]
        first = bootstrap_ci(deltas, seed=1)
        second = bootstrap_ci(deltas, seed=2)
        assert first is not None and second is not None
        assert abs(first.low - second.low) < 0.1

    def test_the_interval_brackets_the_mean(self) -> None:
        deltas = [-0.2, -0.1, -0.15, -0.25, -0.3, -0.05, -0.18, -0.22]
        interval = bootstrap_ci(deltas, seed=7)
        assert interval is not None
        assert interval.low <= mean(deltas) <= interval.high

    def test_zero_variance_collapses_the_interval(self) -> None:
        interval = bootstrap_ci([-0.2] * 8, seed=3)
        assert interval is not None
        assert interval.low == interval.high == pytest.approx(-0.2)

    def test_an_impossible_confidence_level_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="confidence"):
            bootstrap_ci([-0.2] * 8, seed=3, confidence=1.5)


class TestInstability:
    def test_no_replicates_means_no_measured_instability(self) -> None:
        assert instability([]) == 0.0

    def test_agreeing_replicates_are_stable(self) -> None:
        assert instability([(0.5, 0.5), (0.7, 0.7)]) == 0.0

    def test_the_worst_disagreement_is_reported(self) -> None:
        assert instability([(0.5, 0.5), (0.7, 0.9)]) == pytest.approx(0.2)


class TestSummarize:
    def test_an_empty_arm_summarizes_to_zero(self) -> None:
        summary = summarize(RunKind.CONTROL, [])
        assert summary.n == 0
        assert summary.scores == []

    def test_summary_preserves_run_ids_for_traceability(self, scope) -> None:
        from datetime import UTC, datetime

        from backend.core.schemas import ExperimentRun

        runs = [
            ExperimentRun(
                id=f"RUN-{i}", experiment_id="EXP-1", kind=RunKind.BASELINE, index=i,
                scope=scope, features={"urgency_marker": 0.9, "signal_b": 0.2, "signal_c": 0.5},
                score=0.5 + i / 100, decision="HIGH", model_explanation="x",
                input_hash="sha256:a", output_hash="sha256:b",
                executed_at=datetime(2026, 1, 1, tzinfo=UTC), duration_ms=1.0,
            )
            for i in range(3)
        ]
        summary = summarize(RunKind.BASELINE, runs)
        assert summary.run_ids == ["RUN-0", "RUN-1", "RUN-2"]
        assert summary.n == 3
        assert summary.minimum == pytest.approx(0.5)
        assert summary.maximum == pytest.approx(0.52)
