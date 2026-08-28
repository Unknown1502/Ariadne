"""Synthetic laboratory tests (prompt 03).

Two properties matter here, and they pull in opposite directions:

  - The lab must be perfectly reproducible, or evidence gathered today cannot be compared
    with evidence gathered after a model change.
  - The lab must still contain a genuinely hard case (v3), or the verifier is never asked
    to say "I don't know" and INCONCLUSIVE becomes decorative.

The effect-size assertions are the demo's ground truth. If one of them starts failing, the
demo's headline results (v1 contradicted, v2 supported, v3 inconclusive, v4 contradicted)
are no longer true, which is exactly when a test should fail loudly.
"""

from __future__ import annotations

from statistics import mean

import pytest

from backend.core.errors import ValidationError
from backend.experiment_engine.distributions import (
    DISTRIBUTIONS,
    FEATURE_NAMES,
    get_distribution,
    get_fixture_set,
    neutral_value,
    validate_features,
)
from backend.experiment_engine.target_model import (
    HIGH_PRIORITY_THRESHOLD,
    KNOWN_VERSIONS,
    STANDING_EXPLANATION,
    SyntheticTriageModel,
    TargetModel,
    UnstableTriageModel,
    describe_version,
    get_target_model,
)

EFFECT_THRESHOLD = 0.10
BASELINE = "baseline_2024.1"
SHIFTED = "shifted_2025.2"


def observed_rate(version: str, distribution: str, n: int = 24) -> float:
    """Fraction of cases where neutralizing urgency lowers the score past the threshold."""
    model = get_target_model(version, distribution)
    cases = get_fixture_set(
        "triage_baseline_v1" if distribution == BASELINE else "triage_shifted_v1"
    ).cases(n)
    deltas = [
        model.predict({**c.features, "urgency_marker": neutral_value("urgency_marker")}).score
        - model.predict(c.features).score
        for c in cases
    ]
    return sum(1 for d in deltas if d <= -EFFECT_THRESHOLD) / len(deltas)


def mean_effect(version: str, variable: str, distribution: str = BASELINE, n: int = 24) -> float:
    model = get_target_model(version, distribution)
    cases = get_fixture_set(
        "triage_baseline_v1" if distribution == BASELINE else "triage_shifted_v1"
    ).cases(n)
    return mean(
        model.predict({**c.features, variable: neutral_value(variable)}).score
        - model.predict(c.features).score
        for c in cases
    )


class TestInterface:
    def test_the_model_satisfies_the_protocol(self) -> None:
        assert isinstance(get_target_model("1.0.0"), TargetModel)

    def test_every_version_carries_its_metadata(self) -> None:
        for version in KNOWN_VERSIONS:
            model = get_target_model(version, SHIFTED)
            assert model.version == version
            assert model.model_id == "synthetic-triage"
            assert model.distribution_version == SHIFTED

    def test_every_version_emits_the_same_standing_explanation(self) -> None:
        # The explanation is the constant; the model underneath it is the variable.
        cases = get_fixture_set("triage_baseline_v1").cases(3)
        for version in KNOWN_VERSIONS:
            model = get_target_model(version)
            for case in cases:
                assert model.predict(case.features).explanation == STANDING_EXPLANATION

    def test_the_published_description_is_checkable_by_hand(self) -> None:
        described = describe_version("1.0.0")
        assert "0.2*urgency_marker" in str(described["formula"])
        assert "no clinical validity" in str(described["disclaimer"])


class TestDeterminism:
    def test_same_input_and_version_produce_the_same_output(self) -> None:
        cases = get_fixture_set("triage_baseline_v1").cases(8)
        for version in KNOWN_VERSIONS:
            first = get_target_model(version)
            second = get_target_model(version)
            for case in cases:
                assert first.predict(case.features) == second.predict(case.features)

    def test_repeated_calls_on_one_instance_do_not_drift(self) -> None:
        model = get_target_model("3.0.0")  # the noisy version is the one at risk
        case = get_fixture_set("triage_baseline_v1").cases(1)[0]
        scores = {model.predict(case.features).score for _ in range(50)}
        assert len(scores) == 1, "a pure model must not change its answer between calls"

    def test_fixture_cases_are_stable_regardless_of_set_size(self) -> None:
        small = get_fixture_set("triage_baseline_v1").cases(4)
        large = get_fixture_set("triage_baseline_v1").cases(32)
        assert [c.features for c in small] == [c.features for c in large[:4]]

    def test_fixture_cases_lie_inside_their_distribution(self) -> None:
        for name, distribution in DISTRIBUTIONS.items():
            fixture = get_fixture_set(
                "triage_baseline_v1" if name == BASELINE else "triage_shifted_v1"
            )
            for case in fixture.cases(16):
                for feature in FEATURE_NAMES:
                    low, high = distribution.ranges[feature]
                    assert low <= case.features[feature] <= high

    def test_version_three_is_rough_not_random(self) -> None:
        # Its perturbation is keyed by the input, so different cases differ but any single
        # case is fixed forever.
        model = get_target_model("3.0.0")
        cases = get_fixture_set("triage_baseline_v1").cases(16)
        scores = [model.predict(c.features).score for c in cases]
        assert len(set(scores)) > 1


class TestValidation:
    def test_unknown_version_fails_closed(self) -> None:
        with pytest.raises(ValidationError, match="unknown model version"):
            get_target_model("9.9.9")

    def test_unknown_distribution_fails_closed(self) -> None:
        with pytest.raises(ValidationError, match="unknown distribution_version"):
            get_target_model("1.0.0", "not_a_distribution")

    def test_missing_feature_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="missing"):
            get_target_model("1.0.0").predict({"urgency_marker": 0.9, "signal_b": 0.2})

    def test_unknown_feature_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unknown features"):
            get_target_model("1.0.0").predict(
                {"urgency_marker": 0.9, "signal_b": 0.2, "signal_c": 0.5, "backdoor": 1.0}
            )

    def test_out_of_range_feature_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="outside its realistic range"):
            get_target_model("1.0.0").predict(
                {"urgency_marker": 5.0, "signal_b": 0.2, "signal_c": 0.5}
            )

    def test_non_numeric_feature_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be numeric"):
            validate_features({"urgency_marker": "high", "signal_b": 0.2, "signal_c": 0.5})

    def test_booleans_are_not_accepted_as_numbers(self) -> None:
        with pytest.raises(ValidationError, match="must be numeric"):
            validate_features({"urgency_marker": True, "signal_b": 0.2, "signal_c": 0.5})

    def test_oversized_fixture_request_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="silently generating extra cases"):
            get_fixture_set("triage_baseline_v1").cases(10_000)


class TestDecisionBoundary:
    def test_decision_follows_the_published_threshold(self) -> None:
        model = get_target_model("2.0.0")
        for case in get_fixture_set("triage_baseline_v1").cases(24):
            out = model.predict(case.features)
            expected = (
                "HIGH_PRIORITY" if out.score >= HIGH_PRIORITY_THRESHOLD else "STANDARD_PRIORITY"
            )
            assert out.decision == expected

    def test_scores_stay_inside_the_unit_interval(self) -> None:
        for version in KNOWN_VERSIONS:
            model = get_target_model(version)
            for case in get_fixture_set("triage_baseline_v1").cases(24):
                assert 0.0 <= model.predict(case.features).score <= 1.0


class TestGroundTruth:
    """The four demo results, derived from the formulas rather than asserted by hand."""

    def test_v1_urgency_effect_never_clears_the_threshold(self) -> None:
        assert observed_rate("1.0.0", BASELINE) == 0.0

    def test_v1_control_beats_the_claimed_driver(self) -> None:
        assert abs(mean_effect("1.0.0", "signal_c")) > abs(mean_effect("1.0.0", "urgency_marker"))

    def test_v2_urgency_effect_is_reproducible(self) -> None:
        assert observed_rate("2.0.0", BASELINE) == 1.0

    def test_v2_control_is_weaker_than_the_claimed_driver(self) -> None:
        assert abs(mean_effect("2.0.0", "signal_c")) < abs(mean_effect("2.0.0", "urgency_marker"))

    def test_v3_is_genuinely_ambiguous(self) -> None:
        # Neither reproducibly present nor reproducibly absent: the honest answer is
        # "insufficient evidence", and this is the case that proves the system can say it.
        rate = observed_rate("3.0.0", BASELINE)
        assert 0.20 < rate < 0.80, f"v3 observed rate {rate} is no longer ambiguous"

    def test_v4_urgency_main_effect_stays_below_the_threshold(self) -> None:
        assert observed_rate("4.0.0", BASELINE) <= 0.05

    def test_v4_control_beats_the_claimed_driver(self) -> None:
        assert abs(mean_effect("4.0.0", "signal_c")) > abs(mean_effect("4.0.0", "urgency_marker"))

    @pytest.mark.parametrize("n", [8, 12, 16, 24, 32, 64])
    def test_ground_truth_does_not_depend_on_sample_size(self, n: int) -> None:
        # A result that flips when someone changes `repetitions` is not a result.
        assert observed_rate("1.0.0", BASELINE, n) == 0.0
        assert observed_rate("2.0.0", BASELINE, n) == 1.0
        assert 0.20 < observed_rate("3.0.0", BASELINE, n) < 0.80
        assert observed_rate("4.0.0", BASELINE, n) <= 0.05


class TestDistributionShift:
    def test_the_shifted_distribution_makes_the_intervention_tiny(self) -> None:
        # Urgency now sits near its own neutral value, so "neutralize urgency" barely
        # changes the input. That is why the shift makes the claim untestable rather than
        # false, and why the verifier should return INCONCLUSIVE and not CONTRADICTED.
        cases = get_fixture_set("triage_shifted_v1").cases(24)
        perturbation = mean(
            abs(neutral_value("urgency_marker") - c.features["urgency_marker"]) for c in cases
        )
        assert perturbation < 0.10

    def test_the_baseline_distribution_supports_a_real_intervention(self) -> None:
        cases = get_fixture_set("triage_baseline_v1").cases(24)
        perturbation = mean(
            abs(neutral_value("urgency_marker") - c.features["urgency_marker"]) for c in cases
        )
        assert perturbation > 0.15

    def test_the_two_distributions_are_genuinely_different(self) -> None:
        assert get_distribution(BASELINE).ranges != get_distribution(SHIFTED).ranges


class TestUnstableDouble:
    def test_the_unstable_double_really_is_unstable(self) -> None:
        # Guards the guard: the instability test is only meaningful if this drifts.
        model = UnstableTriageModel()
        case = get_fixture_set("triage_baseline_v1").cases(1)[0]
        scores = {model.predict(case.features).score for _ in range(10)}
        assert len(scores) > 1

    def test_the_unstable_double_is_not_a_registered_version(self) -> None:
        assert not isinstance(get_target_model("1.0.0"), UnstableTriageModel)
        assert all(
            isinstance(get_target_model(v), SyntheticTriageModel) for v in KNOWN_VERSIONS
        )
