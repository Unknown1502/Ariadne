"""Verifier ground truth and mutation tests (prompt 06).

Ground truth here is derived from the published model formulas, never from a language
model. Each expected verdict is a consequence of arithmetic a reader can check by hand
against `docs/` - which is the only reason it is legitimate to call these "correct".

The mutation tests are the important half. Any rule engine can produce the right answer on
the happy path; what matters is whether the verdict *moves* when the evidence moves, and
only in the direction it should.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.core.clock import ManualClock
from backend.core.enums import ExpectedDirection, VerdictStatus
from backend.core.errors import ValidationError
from backend.experiment_engine.runner import ExperimentRunner
from backend.experiment_engine.target_model import UnstableTriageModel
from backend.verifier.verifier import ReasonCode, generate_verdict, verify
from tests.factories import make_case, make_claim, make_plan

T0 = datetime(2026, 1, 1, tzinfo=UTC)

EXPECTED_VERDICTS = {
    "1.0.0": VerdictStatus.CONTRADICTED,
    "2.0.0": VerdictStatus.SUPPORTED,
    "3.0.0": VerdictStatus.INCONCLUSIVE,
    "4.0.0": VerdictStatus.CONTRADICTED,
}


@pytest.fixture
def runner() -> ExperimentRunner:
    return ExperimentRunner(clock=ManualClock(T0))


def run_and_verify(runner: ExperimentRunner, version: str, distribution="baseline_2024.1", **kw):
    claim, plan = make_case(version, distribution, **kw)
    result = runner.run(plan, claim)
    return verify(result.evidence, claim, plan), result, claim, plan


class TestTheFourVersions:
    """The demo's headline results. If these fail, the demo is telling a false story."""

    @pytest.mark.parametrize("version,expected", sorted(EXPECTED_VERDICTS.items()))
    def test_verdict_matches_ground_truth(
        self, runner: ExperimentRunner, version: str, expected: VerdictStatus
    ) -> None:
        outcome, *_ = run_and_verify(runner, version)
        assert outcome.status is expected

    def test_v1_is_contradicted_because_the_control_wins(self, runner: ExperimentRunner) -> None:
        outcome, *_ = run_and_verify(runner, "1.0.0")
        assert ReasonCode.PRIMACY_REFUTED in outcome.reason_codes
        assert abs(outcome.control_effect_size) > abs(outcome.effect_size)

    def test_v2_support_is_backed_by_a_weaker_control(self, runner: ExperimentRunner) -> None:
        outcome, *_ = run_and_verify(runner, "2.0.0")
        assert outcome.reproducibility == 1.0
        assert abs(outcome.control_effect_size) < abs(outcome.effect_size)
        assert outcome.contradiction_score == 0.0

    def test_v3_is_inconclusive_rather_than_contradicted(self, runner: ExperimentRunner) -> None:
        # The important negative: an ambiguous result must not be reported as refutation.
        outcome, *_ = run_and_verify(runner, "3.0.0")
        assert outcome.status is VerdictStatus.INCONCLUSIVE
        assert ReasonCode.EFFECT_NOT_REPRODUCIBLE in outcome.reason_codes
        assert 0.20 < outcome.reproducibility < 0.80

    def test_v4_interaction_still_refutes_primacy(self, runner: ExperimentRunner) -> None:
        outcome, *_ = run_and_verify(runner, "4.0.0")
        assert outcome.status is VerdictStatus.CONTRADICTED
        assert ReasonCode.PRIMACY_REFUTED in outcome.reason_codes

    @pytest.mark.parametrize("version", sorted(EXPECTED_VERDICTS))
    def test_every_verdict_is_reproducible(self, runner: ExperimentRunner, version: str) -> None:
        first, *_ = run_and_verify(runner, version)
        second, *_ = run_and_verify(runner, version)
        assert first == second

    @pytest.mark.parametrize("repetitions", [8, 12, 16, 24, 32])
    def test_verdicts_do_not_depend_on_sample_size(
        self, runner: ExperimentRunner, repetitions: int
    ) -> None:
        for version, expected in EXPECTED_VERDICTS.items():
            outcome, *_ = run_and_verify(runner, version, repetitions=repetitions)
            assert outcome.status is expected, f"v{version} flipped at n={repetitions}"


class TestDistributionShift:
    def test_the_shift_makes_a_supported_claim_untestable_not_false(
        self, runner: ExperimentRunner
    ) -> None:
        # v2's explanation was SUPPORTED under the original distribution. After the shift
        # the probe can no longer move the input enough to test it. The honest answer is
        # INCONCLUSIVE; reporting CONTRADICTED here would be a fabricated refutation.
        before, *_ = run_and_verify(runner, "2.0.0")
        after, *_ = run_and_verify(runner, "2.0.0", "shifted_2025.2")
        assert before.status is VerdictStatus.SUPPORTED
        assert after.status is VerdictStatus.INCONCLUSIVE
        assert ReasonCode.WEAK_PERTURBATION in after.reason_codes
        assert ReasonCode.INVALID_INTERVENTION in after.reason_codes

    def test_validity_reports_a_meaningful_number(self, runner: ExperimentRunner) -> None:
        after, *_ = run_and_verify(runner, "2.0.0", "shifted_2025.2")
        assert 0.1 < after.intervention_validity < 0.5


class TestGatePrecedence:
    """Invalid probes must be caught before the data is interpreted."""

    def test_an_invalid_probe_never_contradicts(self, runner: ExperimentRunner) -> None:
        claim, plan = make_case("1.0.0")
        # v1 would otherwise be CONTRADICTED; break the probe and it must go INCONCLUSIVE.
        broken = plan.model_copy(update={"validity_threshold": 1.0})
        result = runner.run(broken, claim)
        weakened = result.evidence.model_copy(update={"validity_score": 0.4})
        outcome = verify(weakened, claim, broken)
        assert outcome.status is VerdictStatus.INCONCLUSIVE
        assert ReasonCode.INVALID_INTERVENTION in outcome.reason_codes

    def test_too_few_runs_is_inconclusive(self, runner: ExperimentRunner) -> None:
        claim, plan = make_case("2.0.0", repetitions=3, min_repetitions_for_verdict=3)
        result = runner.run(plan, claim)
        stricter = plan.model_copy(update={"min_repetitions_for_verdict": 10})
        outcome = verify(result.evidence, claim, stricter)
        assert outcome.status is VerdictStatus.INCONCLUSIVE
        assert ReasonCode.INSUFFICIENT_RUNS in outcome.reason_codes

    def test_an_unstable_target_model_blocks_any_verdict(self) -> None:
        # A model that answers differently on identical input cannot be probed by a paired
        # design, so no verdict is issued at all.
        unstable = ExperimentRunner(
            clock=ManualClock(T0), model_factory=lambda v, d: UnstableTriageModel(v)
        )
        claim, plan = make_case("2.0.0")
        result = unstable.run(plan, claim)
        outcome = verify(result.evidence, claim, plan)
        assert result.evidence.instability > plan.instability_threshold
        assert outcome.status is VerdictStatus.INCONCLUSIVE
        assert ReasonCode.MODEL_UNSTABLE in outcome.reason_codes

    def test_a_vague_claim_is_inconclusive(self, runner: ExperimentRunner) -> None:
        claim, plan = make_case("2.0.0")
        vague = claim.model_copy(update={"testability_score": 0.1})
        result = runner.run(plan, claim)
        outcome = verify(result.evidence, vague, plan)
        assert outcome.status is VerdictStatus.INCONCLUSIVE
        assert ReasonCode.VAGUE_CLAIM in outcome.reason_codes


class TestMutations:
    """Change one input at a time and confirm the verdict responds correctly."""

    def test_lowering_the_effect_threshold_exposes_the_control_comparison(
        self, runner: ExperimentRunner
    ) -> None:
        # With a low threshold, v1's small urgency effect *is* reproducible - but the
        # control still moves the score more, so a primacy claim remains refuted.
        claim, plan = make_case("1.0.0", min_effect_threshold=0.02)
        result = runner.run(plan, claim)
        outcome = verify(result.evidence, claim, plan)
        assert outcome.reproducibility >= 0.8
        assert outcome.status is VerdictStatus.CONTRADICTED
        assert ReasonCode.CONTROL_DOMINATES in outcome.reason_codes

    def test_dropping_the_primacy_assertion_changes_the_same_evidence_to_support(
        self, runner: ExperimentRunner
    ) -> None:
        # Same numbers, weaker claim. "Urgency has an effect" survives evidence that
        # "urgency is the primary driver" does not.
        claim = make_claim("1.0.0").model_copy(update={"primacy_claim": False})
        plan = make_plan(claim, min_effect_threshold=0.02)
        result = runner.run(plan, claim)
        outcome = verify(result.evidence, claim, plan)
        assert outcome.status is VerdictStatus.SUPPORTED

    def test_removing_the_control_removes_the_primacy_test(
        self, runner: ExperimentRunner
    ) -> None:
        claim, plan = make_case("1.0.0", control=None, min_effect_threshold=0.02)
        result = runner.run(plan, claim)
        outcome = verify(result.evidence, claim, plan)
        assert outcome.control_effect_size is None
        assert ReasonCode.CONTROL_ABSENT in outcome.reason_codes
        assert outcome.status is VerdictStatus.SUPPORTED

    def test_flipping_the_expected_direction_withdraws_support(
        self, runner: ExperimentRunner
    ) -> None:
        claim, plan = make_case("2.0.0")
        result = runner.run(plan, claim)
        inverted = plan.model_copy(update={"expected_direction": ExpectedDirection.INCREASE})
        outcome = verify(result.evidence, claim, inverted)
        assert outcome.status is not VerdictStatus.SUPPORTED

    def test_raising_the_reproducibility_bar_withdraws_support(
        self, runner: ExperimentRunner
    ) -> None:
        claim, plan = make_case("3.0.0")
        result = runner.run(plan, claim)
        lenient = plan.model_copy(update={"reproducibility_threshold": 0.5})
        outcome = verify(result.evidence, claim, lenient)
        # v3 clears a 0.5 bar but not the default 0.8 one - and the verdict says so.
        assert outcome.status is VerdictStatus.SUPPORTED
        strict = plan.model_copy(update={"reproducibility_threshold": 0.95})
        assert verify(result.evidence, claim, strict).status is VerdictStatus.INCONCLUSIVE

    def test_zeroing_validity_cannot_produce_support(self, runner: ExperimentRunner) -> None:
        claim, plan = make_case("2.0.0")
        result = runner.run(plan, claim)
        invalid = result.evidence.model_copy(update={"validity_score": 0.0})
        assert verify(invalid, claim, plan).status is VerdictStatus.INCONCLUSIVE

    def test_behavioral_support_is_discounted_by_a_weak_probe(
        self, runner: ExperimentRunner
    ) -> None:
        claim, plan = make_case("2.0.0")
        result = runner.run(plan, claim)
        full = verify(result.evidence, claim, plan)
        degraded = verify(
            result.evidence.model_copy(update={"validity_score": 0.95}),
            claim,
            plan.model_copy(update={"validity_threshold": 0.9}),
        )
        assert degraded.behavioral_support < full.behavioral_support


class TestScopeSafety:
    def test_evidence_from_another_claim_is_refused(self, runner: ExperimentRunner) -> None:
        claim, plan = make_case("1.0.0")
        result = runner.run(plan, claim)
        other = make_claim("2.0.0")
        with pytest.raises(ValidationError, match="belongs to claim"):
            verify(result.evidence, other, plan)

    def test_evidence_from_another_experiment_is_refused(
        self, runner: ExperimentRunner
    ) -> None:
        claim, plan = make_case("1.0.0")
        result = runner.run(plan, claim)
        foreign = result.evidence.model_copy(update={"experiment_id": "EXP-somewhere-else"})
        with pytest.raises(ValidationError, match="came from experiment"):
            verify(foreign, claim, plan)

    def test_mismatched_version_scope_is_refused(self, runner: ExperimentRunner) -> None:
        claim, plan = make_case("1.0.0")
        result = runner.run(plan, claim)
        mis_scoped = result.evidence.model_copy(
            update={"scope": claim.scope.model_copy(update={"model_version": "2.0.0"})}
        )
        with pytest.raises(ValidationError, match="scope mismatch"):
            verify(mis_scoped, claim, plan)


class TestVerdictRecord:
    def test_generated_verdict_carries_full_provenance(self, runner: ExperimentRunner) -> None:
        claim, plan = make_case("1.0.0")
        result = runner.run(plan, claim)
        verdict = generate_verdict(result.evidence, claim, plan, created_at=T0)

        assert verdict.status is VerdictStatus.CONTRADICTED
        assert verdict.evidence_ids == [result.evidence.id]
        assert verdict.verifier_version == "1.0.0"
        assert verdict.scope.matches(claim.scope)
        assert verdict.protocol_version == plan.protocol_version
        assert verdict.reason_codes

    def test_rationale_is_composed_from_the_computed_numbers(
        self, runner: ExperimentRunner
    ) -> None:
        claim, plan = make_case("1.0.0")
        result = runner.run(plan, claim)
        verdict = generate_verdict(result.evidence, claim, plan, created_at=T0)
        assert f"{verdict.effect_size:+.4f}" in verdict.rationale
        assert "verifier=1.0.0" in verdict.rationale
        for code in verdict.reason_codes:
            assert code in verdict.rationale

    def test_verdict_ids_are_stable_for_identical_evidence(
        self, runner: ExperimentRunner
    ) -> None:
        claim, plan = make_case("1.0.0")
        first = generate_verdict(runner.run(plan, claim).evidence, claim, plan, created_at=T0)
        second = generate_verdict(runner.run(plan, claim).evidence, claim, plan, created_at=T0)
        assert first.id == second.id

    def test_the_verifier_imports_no_language_model(self) -> None:
        # Structural guarantee, not a promise in a docstring: the module that decides
        # verdicts must not be able to call a model even by accident.
        import backend.verifier.verifier as module

        source = (module.__file__ or "")
        assert source
        text = Path(source).read_text(encoding="utf-8")
        for forbidden in ("genai", "gemini", "openai", "llm_client", "LLMClient"):
            assert forbidden not in text, f"verifier references {forbidden}"
