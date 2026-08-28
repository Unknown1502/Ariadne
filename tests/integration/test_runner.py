"""Experiment engine tests (prompt 05).

Covers the three guarantees the runner exists to provide: version binding, resumability,
and provenance - plus the failure paths, because "what happens when the target model dies
halfway" is a question the demo has to be able to answer.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.core.clock import ManualClock
from backend.core.enums import RunKind
from backend.core.errors import (
    InterventionRejected,
    TargetModelError,
    ValidationError,
    VersionMismatch,
)
from backend.experiment_engine.runner import (
    ExperimentRunner,
    InMemoryRunStore,
)
from backend.experiment_engine.target_model import (
    FailingTargetModel,
    SyntheticTriageModel,
    get_target_model,
)
from tests.factories import make_case, make_claim, make_plan

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def runner() -> ExperimentRunner:
    return ExperimentRunner(clock=ManualClock(T0))


class TestExecution:
    def test_runs_every_arm_for_every_case(self, runner: ExperimentRunner) -> None:
        claim, plan = make_case("1.0.0", repetitions=8)
        result = runner.run(plan, claim)
        kinds = [run.kind for run in result.runs]
        assert kinds.count(RunKind.BASELINE) == 8
        assert kinds.count(RunKind.INTERVENTION) == 8
        assert kinds.count(RunKind.CONTROL) == 8

    def test_omits_the_control_arm_when_no_control_is_planned(
        self, runner: ExperimentRunner
    ) -> None:
        claim, plan = make_case("1.0.0", repetitions=8, control=None)
        result = runner.run(plan, claim)
        assert not [r for r in result.runs if r.kind is RunKind.CONTROL]
        assert result.evidence.control is None

    def test_arms_are_paired_on_the_same_cases(self, runner: ExperimentRunner) -> None:
        # Pairing is what removes between-case variance. If the arms drifted apart, the
        # deltas would be meaningless while still looking fine.
        claim, plan = make_case("1.0.0", repetitions=6)
        result = runner.run(plan, claim)
        baseline = {r.index: r for r in result.runs if r.kind is RunKind.BASELINE}
        intervention = {r.index: r for r in result.runs if r.kind is RunKind.INTERVENTION}
        assert set(baseline) == set(intervention)
        for index, base_run in baseline.items():
            other = intervention[index]
            assert base_run.features["signal_b"] == other.features["signal_b"]
            assert base_run.features["signal_c"] == other.features["signal_c"]
            assert other.features["urgency_marker"] == 0.5

    def test_evidence_is_reproducible(self, runner: ExperimentRunner) -> None:
        claim, plan = make_case("1.0.0")
        first = runner.run(plan, claim).evidence
        second = ExperimentRunner(clock=ManualClock(T0)).run(plan, claim).evidence
        assert first.evidence_hash == second.evidence_hash
        assert first.id == second.id
        assert first.effect_size == second.effect_size

    def test_every_run_records_input_and_output_hashes(self, runner: ExperimentRunner) -> None:
        claim, plan = make_case("1.0.0", repetitions=4)
        result = runner.run(plan, claim)
        for run in result.runs:
            assert run.input_hash.startswith("sha256:")
            assert run.output_hash.startswith("sha256:")
        assert len(result.evidence.input_hashes) == len(result.runs)

    def test_evidence_binds_the_model_and_distribution_version(
        self, runner: ExperimentRunner
    ) -> None:
        claim, plan = make_case("2.0.0")
        evidence = runner.run(plan, claim).evidence
        assert evidence.scope.model_version == "2.0.0"
        assert evidence.scope.distribution_version == "baseline_2024.1"

    def test_a_confidence_interval_is_reported_when_there_is_enough_data(
        self, runner: ExperimentRunner
    ) -> None:
        claim, plan = make_case("2.0.0")
        evidence = runner.run(plan, claim).evidence
        assert evidence.effect_ci is not None
        low, high = evidence.effect_ci
        assert low <= evidence.effect_size <= high

    def test_instability_is_zero_for_a_deterministic_model(
        self, runner: ExperimentRunner
    ) -> None:
        claim, plan = make_case("3.0.0")  # the noisy version is still a pure function
        assert runner.run(plan, claim).evidence.instability == 0.0


class TestVersionBinding:
    def test_a_plan_cannot_run_against_a_different_model_version(
        self, runner: ExperimentRunner
    ) -> None:
        claim = make_claim("1.0.0")
        plan = make_plan(make_claim("2.0.0"))
        with pytest.raises(ValidationError):
            runner.run(plan, claim)

    def test_scope_drift_between_claim_and_plan_is_refused(
        self, runner: ExperimentRunner
    ) -> None:
        claim, plan = make_case("1.0.0")
        drifted = plan.model_copy(
            update={"scope": plan.scope.model_copy(update={"model_version": "2.0.0"})}
        )
        with pytest.raises(VersionMismatch, match="attributed to the wrong model"):
            runner.run(drifted, claim)

    def test_a_fixture_set_from_another_distribution_is_refused(
        self, runner: ExperimentRunner
    ) -> None:
        claim, plan = make_case("1.0.0")
        wrong = plan.model_copy(update={"fixture_set": "triage_shifted_v1"})
        with pytest.raises(VersionMismatch, match="draws from"):
            runner.run(wrong, claim)

    def test_a_model_factory_returning_the_wrong_version_is_caught(self) -> None:
        # Defence in depth: even if resolution is wired up wrongly, the run stops.
        liar = ExperimentRunner(
            clock=ManualClock(T0), model_factory=lambda v, d: SyntheticTriageModel("2.0.0", d)
        )
        claim, plan = make_case("1.0.0")
        with pytest.raises(VersionMismatch, match="resolved model is"):
            liar.run(plan, claim)


class TestPlanAdmission:
    def test_a_plan_for_another_claim_is_refused(self, runner: ExperimentRunner) -> None:
        claim, _ = make_case("1.0.0")
        other_claim = make_claim("1.0.0", investigation_id="INV-other")
        plan = make_plan(other_claim).model_copy(update={"claim_id": "CLM-elsewhere"})
        with pytest.raises(ValidationError, match="targets claim"):
            runner.run(plan, claim)

    def test_intervening_on_an_unclaimed_variable_is_refused(
        self, runner: ExperimentRunner
    ) -> None:
        from backend.core.enums import InterventionType
        from backend.core.schemas import InterventionSpec

        claim, plan = make_case("1.0.0")
        rogue = plan.model_copy(
            update={
                "intervention": InterventionSpec(
                    variable="signal_b",
                    intervention_type=InterventionType.NEUTRALIZE,
                    value=0.5,
                ),
                "constraints": plan.constraints.model_copy(
                    update={"preserved_features": ["signal_c"]}
                ),
            }
        )
        with pytest.raises(InterventionRejected, match="never named as a target variable"):
            runner.run(rogue, claim)

    def test_a_control_on_a_claimed_variable_is_refused(
        self, runner: ExperimentRunner
    ) -> None:
        from backend.core.enums import InterventionType
        from backend.core.schemas import InterventionSpec

        claim = make_claim("1.0.0").model_copy(
            update={"target_variables": ["urgency_marker", "signal_c"]}
        )
        plan = make_plan(claim).model_copy(
            update={
                "control": InterventionSpec(
                    variable="signal_c",
                    intervention_type=InterventionType.NEUTRALIZE,
                    value=0.5,
                )
            }
        )
        with pytest.raises(InterventionRejected, match="that is not a control"):
            runner.run(plan, claim)

    def test_a_quarantined_claim_is_never_executed(self, runner: ExperimentRunner) -> None:
        claim, plan = make_case("1.0.0")
        poisoned = claim.model_copy(
            update={"quarantined": True, "quarantine_reasons": ["PROMPT_INJECTION"]}
        )
        with pytest.raises(InterventionRejected, match="quarantined"):
            runner.run(plan, poisoned)

    def test_nothing_is_executed_when_admission_fails(self) -> None:
        # The plan check must happen before the first model call, or a rejected plan still
        # leaves runs in the store.
        store = InMemoryRunStore()
        runner = ExperimentRunner(clock=ManualClock(T0), run_store=store)
        claim, plan = make_case("1.0.0")
        drifted = plan.model_copy(
            update={"scope": plan.scope.model_copy(update={"model_version": "3.0.0"})}
        )
        with pytest.raises(VersionMismatch):
            runner.run(drifted, claim)
        assert store.completed_runs(drifted.id) == {}


class TestResumability:
    def test_a_resumed_run_reuses_its_checkpoint(self) -> None:
        store = InMemoryRunStore()
        claim, plan = make_case("1.0.0", repetitions=8)

        first = ExperimentRunner(clock=ManualClock(T0), run_store=store).run(plan, claim)
        assert first.runs_reused == 0

        second = ExperimentRunner(clock=ManualClock(T0), run_store=store).run(plan, claim)
        assert second.runs_reused == len(second.runs)
        assert second.evidence.evidence_hash == first.evidence.evidence_hash

    def test_resuming_after_a_partial_run_completes_the_rest(self) -> None:
        # Simulates a worker that died mid-experiment: some runs are checkpointed, the
        # rest are not, and the resumed worker must finish rather than restart.
        store = InMemoryRunStore()
        claim, plan = make_case("1.0.0", repetitions=8)
        complete = ExperimentRunner(clock=ManualClock(T0), run_store=store).run(plan, claim)

        partial = InMemoryRunStore()
        for run in complete.runs[:5]:
            partial.record_run(run)

        resumed = ExperimentRunner(clock=ManualClock(T0), run_store=partial).run(plan, claim)
        assert resumed.runs_reused == 5
        assert resumed.evidence.evidence_hash == complete.evidence.evidence_hash

    def test_resumption_does_not_inflate_the_sample(self) -> None:
        store = InMemoryRunStore()
        claim, plan = make_case("1.0.0", repetitions=8)
        ExperimentRunner(clock=ManualClock(T0), run_store=store).run(plan, claim)
        second = ExperimentRunner(clock=ManualClock(T0), run_store=store).run(plan, claim)
        assert second.evidence.baseline.n == 8
        assert second.evidence.intervention.n == 8


class TestFailurePaths:
    def test_a_failing_target_model_raises_a_retryable_error(self) -> None:
        runner = ExperimentRunner(
            clock=ManualClock(T0), model_factory=lambda v, d: FailingTargetModel(v)
        )
        claim, plan = make_case("1.0.0")
        with pytest.raises(TargetModelError) as caught:
            runner.run(plan, claim)
        assert caught.value.retryable is True

    def test_an_invalid_feature_vector_is_not_treated_as_a_transient_failure(self) -> None:
        # A malformed input is a contract bug, not something a retry will fix.
        class BadFixtureModel:
            model_id = "synthetic-triage"
            version = "1.0.0"
            distribution_version = "baseline_2024.1"

            def predict(self, features):
                raise ValidationError("feature vector is missing ['signal_c']")

        runner = ExperimentRunner(
            clock=ManualClock(T0), model_factory=lambda v, d: BadFixtureModel()
        )
        claim, plan = make_case("1.0.0")
        with pytest.raises(ValidationError):
            runner.run(plan, claim)

    def test_partial_progress_survives_a_crash(self) -> None:
        store = InMemoryRunStore()
        calls = {"n": 0}
        real = get_target_model("1.0.0")

        class DyingModel:
            model_id = real.model_id
            version = real.version
            distribution_version = real.distribution_version

            def predict(self, features):
                calls["n"] += 1
                if calls["n"] > 6:
                    raise RuntimeError("worker died")
                return real.predict(features)

        runner = ExperimentRunner(
            clock=ManualClock(T0), run_store=store, model_factory=lambda v, d: DyingModel()
        )
        claim, plan = make_case("1.0.0", repetitions=8)
        with pytest.raises(TargetModelError):
            runner.run(plan, claim)

        checkpointed = store.completed_runs(plan.id)
        assert 0 < len(checkpointed) <= 6

        healthy = ExperimentRunner(clock=ManualClock(T0), run_store=store)
        recovered = healthy.run(plan, claim)
        assert recovered.runs_reused == len(checkpointed)
        assert recovered.evidence.baseline.n == 8
