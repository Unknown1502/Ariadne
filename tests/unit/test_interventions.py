"""Intervention application and validity (prompt 05).

The central distinction under test: a *broken* probe and a *clean probe that found nothing*
must never be confused. The first is INCONCLUSIVE, the second is evidence.
"""

from __future__ import annotations

import pytest

from backend.core.enums import InterventionType
from backend.core.errors import InterventionRejected
from backend.core.schemas import ConstraintSpec, InterventionSpec
from backend.experiment_engine.interventions import (
    MIN_PERTURBATION_FRACTION,
    aggregate_validity,
    apply_intervention,
    resolve_target_value,
    validate_intervention,
)

BASE = {"urgency_marker": 0.90, "signal_b": 0.30, "signal_c": 0.80}
CONSTRAINTS = ConstraintSpec(preserved_features=["signal_b", "signal_c"], tolerance=1e-9)


def neutralize(variable: str = "urgency_marker", value: float = 0.5) -> InterventionSpec:
    return InterventionSpec(
        variable=variable, intervention_type=InterventionType.NEUTRALIZE, value=value
    )


class TestApplication:
    def test_neutralize_sets_the_declared_value(self) -> None:
        result = apply_intervention(BASE, neutralize())
        assert result["urgency_marker"] == 0.5

    def test_the_input_vector_is_never_mutated(self) -> None:
        original = dict(BASE)
        apply_intervention(BASE, neutralize())
        assert original == BASE

    def test_only_the_target_changes(self) -> None:
        result = apply_intervention(BASE, neutralize())
        assert result["signal_b"] == BASE["signal_b"]
        assert result["signal_c"] == BASE["signal_c"]

    def test_relative_interventions_move_from_the_current_value(self) -> None:
        spec = InterventionSpec(
            variable="urgency_marker", intervention_type=InterventionType.DECREASE, delta=-0.2
        )
        assert apply_intervention(BASE, spec)["urgency_marker"] == pytest.approx(0.70)

    def test_ablation_drives_the_feature_to_its_floor(self) -> None:
        spec = InterventionSpec(
            variable="urgency_marker", intervention_type=InterventionType.ABLATION
        )
        assert apply_intervention(BASE, spec)["urgency_marker"] == 0.0

    def test_values_are_clamped_into_the_realistic_range(self) -> None:
        spec = InterventionSpec(
            variable="urgency_marker", intervention_type=InterventionType.INCREASE, delta=5.0
        )
        assert apply_intervention(BASE, spec)["urgency_marker"] == 1.0

    def test_unknown_feature_is_refused(self) -> None:
        with pytest.raises(InterventionRejected, match="unknown feature"):
            apply_intervention(BASE, neutralize("backdoor"))

    def test_absent_feature_is_refused(self) -> None:
        with pytest.raises(InterventionRejected, match="to intervene on"):
            apply_intervention({"urgency_marker": 0.9, "signal_b": 0.3}, neutralize("signal_c"))

    def test_resolve_target_value_is_explicit_per_type(self) -> None:
        assert resolve_target_value(neutralize(), 0.9) == 0.5
        increase = InterventionSpec(
            variable="urgency_marker", intervention_type=InterventionType.INCREASE, delta=0.05
        )
        assert resolve_target_value(increase, 0.9) == pytest.approx(0.95)


class TestValidity:
    def test_a_clean_intervention_is_fully_valid(self) -> None:
        intervened = apply_intervention(BASE, neutralize())
        report = validate_intervention(
            baseline=BASE, intervened=intervened, spec=neutralize(), constraints=CONSTRAINTS
        )
        assert report.score == 1.0
        assert report.reason_codes == ["VALID_INTERVENTION"]
        assert not report.is_hard_failure

    def test_a_mutated_bystander_feature_is_a_hard_failure(self) -> None:
        tampered = apply_intervention(BASE, neutralize())
        tampered["signal_c"] = 0.1  # something perturbed a feature it promised to preserve
        report = validate_intervention(
            baseline=BASE, intervened=tampered, spec=neutralize(), constraints=CONSTRAINTS
        )
        assert report.score == 0.0
        assert report.is_hard_failure
        assert "OUT_OF_SCOPE_MUTATION" in report.reason_codes
        assert "CONSTRAINT_VIOLATION" in report.reason_codes

    def test_an_out_of_range_result_is_a_hard_failure(self) -> None:
        report = validate_intervention(
            baseline=BASE,
            intervened={**BASE, "urgency_marker": 4.2},
            spec=neutralize(),
            constraints=CONSTRAINTS,
        )
        assert report.score == 0.0
        assert "OUT_OF_RANGE" in report.reason_codes

    def test_a_tiny_perturbation_is_weak_rather_than_broken(self) -> None:
        # This is the distribution-shift signature: nothing is malformed, the probe simply
        # did not push hard enough to learn anything.
        intervened = apply_intervention(BASE, neutralize(value=0.88))
        report = validate_intervention(
            baseline=BASE, intervened=intervened, spec=neutralize(value=0.88),
            constraints=CONSTRAINTS,
        )
        assert not report.is_hard_failure
        assert 0.0 < report.score < 1.0
        assert "WEAK_PERTURBATION" in report.reason_codes

    def test_adequacy_is_measured_against_the_declared_fraction(self) -> None:
        moved = MIN_PERTURBATION_FRACTION / 2  # exactly half of what is required
        intervened = apply_intervention(BASE, neutralize(value=BASE["urgency_marker"] - moved))
        report = validate_intervention(
            baseline=BASE,
            intervened=intervened,
            spec=neutralize(value=BASE["urgency_marker"] - moved),
            constraints=CONSTRAINTS,
        )
        assert report.adequacy == pytest.approx(0.5, abs=1e-6)

    def test_a_zero_perturbation_scores_zero(self) -> None:
        report = validate_intervention(
            baseline=BASE, intervened=dict(BASE),
            spec=neutralize(value=BASE["urgency_marker"]), constraints=CONSTRAINTS,
        )
        assert report.score == 0.0
        assert "WEAK_PERTURBATION" in report.reason_codes


class TestAggregation:
    def _clean(self):
        return validate_intervention(
            baseline=BASE, intervened=apply_intervention(BASE, neutralize()),
            spec=neutralize(), constraints=CONSTRAINTS,
        )

    def _broken(self):
        tampered = apply_intervention(BASE, neutralize())
        tampered["signal_c"] = 0.1
        return validate_intervention(
            baseline=BASE, intervened=tampered, spec=neutralize(), constraints=CONSTRAINTS
        )

    def _weak(self):
        spec = neutralize(value=0.88)
        return validate_intervention(
            baseline=BASE, intervened=apply_intervention(BASE, spec), spec=spec,
            constraints=CONSTRAINTS,
        )

    def test_one_broken_case_invalidates_the_whole_probe(self) -> None:
        # Hard failures are combined with AND: a majority of clean cases must not hide one
        # malformed protocol run.
        combined = aggregate_validity([self._clean()] * 23 + [self._broken()])
        assert combined.score == 0.0
        assert combined.is_hard_failure

    def test_one_weak_case_does_not_invalidate_a_strong_probe(self) -> None:
        # Adequacy is averaged: a single case that started near the neutral value is a
        # near-zero delta, which the paired statistics already handle.
        combined = aggregate_validity([self._clean()] * 23 + [self._weak()])
        assert combined.score > 0.9
        assert not combined.is_hard_failure

    def test_uniformly_weak_cases_produce_a_low_score(self) -> None:
        combined = aggregate_validity([self._weak()] * 24)
        assert combined.score < 0.5
        assert "WEAK_PERTURBATION" in combined.reason_codes

    def test_aggregating_nothing_is_refused(self) -> None:
        with pytest.raises(InterventionRejected, match="no cases"):
            aggregate_validity([])

    def test_valid_marker_is_dropped_once_a_real_problem_exists(self) -> None:
        combined = aggregate_validity([self._clean(), self._weak()])
        assert "VALID_INTERVENTION" not in combined.reason_codes
