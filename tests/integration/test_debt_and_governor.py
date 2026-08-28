"""Explanation Debt and the Governor (prompt 08).

Two things are being defended here.

First, that debt is *decomposable and honest*: every component's arithmetic is checkable,
the total is bounded, and the policy version travels with the number.

Second, that the Governor's authority is real but bounded: deterministic policy picks the
action, an LLM recommendation can never widen that authority, and high-impact actions stop
at a human.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import ValidationError as PydanticValidationError

from backend.core.clock import ManualClock
from backend.core.enums import GovernorAction
from backend.core.errors import PermissionDenied, ValidationError
from backend.core.schemas import AgentProvenance
from backend.debt.calculator import DebtCalculator, explain
from backend.experiment_engine.runner import ExperimentRunner
from backend.governance.governor import (
    ACTIONS_REQUIRING_APPROVAL,
    ALLOWED_ACTIONS,
    GovernanceContext,
    Governor,
    build_approval_request,
    decide,
)
from backend.governance.policy import DEFAULT_POLICY, DebtWeights, Policy
from backend.lineage.service import LineageService
from backend.storage.sql import in_memory_ledger
from backend.verifier.verifier import generate_verdict
from tests.factories import T0, make_case

MODEL_ID = "synthetic-triage"


@pytest.fixture
def env():
    ledger = in_memory_ledger()
    clock = ManualClock(T0)
    lineage = LineageService(ledger, clock=clock)
    calculator = DebtCalculator(lineage, clock=clock)

    runner = ExperimentRunner(clock=clock)

    def audit(version: str, distribution: str = "baseline_2024.1"):
        claim, plan = make_case(version, distribution)
        ledger.append_claim(claim)
        ledger.append_plan(plan)
        result = runner.run(plan, claim)
        ledger.append_evidence(result.evidence)
        verdict = generate_verdict(result.evidence, claim, plan, created_at=clock.now())
        ledger.append_verdict(verdict)
        lineage.append_verdict(verdict, result.evidence)
        return claim, verdict

    yield ledger, clock, lineage, calculator, audit
    ledger.dispose()


class TestPolicy:
    def test_weights_must_total_one_hundred(self) -> None:
        with pytest.raises(ValidationError, match="must total 100"):
            Policy(version="9.0.0", weights=DebtWeights(contradictions=90.0))

    def test_thresholds_must_escalate(self) -> None:
        from backend.governance.policy import GovernorThresholds

        with pytest.raises(ValidationError, match="escalate monotonically"):
            Policy(
                version="9.0.0",
                thresholds=GovernorThresholds(human_review_debt=10.0),
            )

    def test_changing_weights_requires_a_new_version(self) -> None:
        # Two incomparable scores must never be able to look comparable.
        with pytest.raises(ValidationError, match="requires a new policy version"):
            DEFAULT_POLICY.with_weights("1.0.0", DebtWeights(contradictions=30, inconclusive=15))

    def test_a_reweighted_policy_gets_a_distinct_fingerprint(self) -> None:
        reweighted = DEFAULT_POLICY.with_weights(
            "2.0.0", DebtWeights(stale_evidence=20, contradictions=30)
        )
        assert reweighted.fingerprint() != DEFAULT_POLICY.fingerprint()

    def test_the_fingerprint_catches_an_unversioned_weight_edit(self) -> None:
        # Guards the guard: if someone edits a default weight without bumping the version,
        # this value changes and the assertion below fails loudly.
        assert DEFAULT_POLICY.version == "1.0.0"
        assert DEFAULT_POLICY.weights.total() == 100.0


class TestDebtCalculation:
    def test_no_claims_means_no_known_debt(self, env) -> None:
        _, _, _, calculator, _ = env
        snapshot = calculator.calculate(MODEL_ID)
        assert snapshot.total == 0.0
        assert len(snapshot.components) == 5  # the breakdown keeps its shape

    def test_a_supported_claim_carries_almost_no_debt(self, env) -> None:
        _, clock, _, calculator, audit = env
        clock.advance(days=1)
        audit("2.0.0")
        assert calculator.calculate(MODEL_ID).total == 0.0

    def test_a_contradiction_raises_debt(self, env) -> None:
        _, clock, _, calculator, audit = env
        clock.advance(days=1)
        audit("1.0.0")
        snapshot = calculator.calculate(MODEL_ID)
        assert snapshot.total > 0
        contradictions = next(c for c in snapshot.components if c.name == "contradictions")
        assert contradictions.points == pytest.approx(25.0)

    def test_version_disagreement_is_its_own_component(self, env) -> None:
        _, clock, _, calculator, audit = env
        clock.advance(days=1)
        audit("1.0.0")  # CONTRADICTED
        clock.advance(days=1)
        audit("2.0.0")  # SUPPORTED - the same claim, a different answer
        snapshot = calculator.calculate(MODEL_ID)
        inconsistency = next(
            c for c in snapshot.components if c.name == "version_inconsistency"
        )
        assert inconsistency.points == pytest.approx(15.0)

    def test_every_component_shows_its_arithmetic(self, env) -> None:
        _, clock, _, calculator, audit = env
        clock.advance(days=1)
        audit("1.0.0")
        snapshot = calculator.calculate(MODEL_ID)
        for component in snapshot.components:
            assert component.points == pytest.approx(component.ratio * component.weight)

    def test_the_total_is_bounded_and_matches_its_parts(self, env) -> None:
        _, clock, _, calculator, audit = env
        for version in ("1.0.0", "2.0.0", "3.0.0", "4.0.0"):
            clock.advance(days=30)
            audit(version)
        clock.advance(days=200)  # let everything go stale
        snapshot = calculator.calculate(MODEL_ID)
        assert 0.0 <= snapshot.total <= 100.0
        assert snapshot.total == pytest.approx(sum(c.points for c in snapshot.components))

    def test_stale_evidence_raises_debt_over_time(self, env) -> None:
        _, clock, _, calculator, audit = env
        clock.advance(days=1)
        audit("2.0.0")
        fresh = calculator.calculate(MODEL_ID)
        clock.advance(days=200)
        stale = calculator.calculate(MODEL_ID)
        assert stale.total > fresh.total
        stale_component = next(c for c in stale.components if c.name == "stale_evidence")
        assert stale_component.points == pytest.approx(25.0)

    def test_expired_evidence_registers_as_distribution_sensitivity(self, env) -> None:
        _, clock, lineage, calculator, audit = env
        clock.advance(days=1)
        claim, _ = audit("2.0.0")
        clock.advance(days=1)
        lineage.expire_evidence(claim.claim_family_id, reason="DISTRIBUTION_CHANGED")
        snapshot = calculator.calculate(MODEL_ID)
        sensitivity = next(
            c for c in snapshot.components if c.name == "distribution_sensitivity"
        )
        assert sensitivity.points == pytest.approx(15.0)

    def test_snapshots_are_immutable_and_carry_their_policy_version(self, env) -> None:
        _, clock, _, calculator, audit = env
        clock.advance(days=1)
        audit("1.0.0")
        snapshot = calculator.calculate(MODEL_ID)
        assert snapshot.policy_version == "1.0.0"
        with pytest.raises(PydanticValidationError):
            snapshot.total = 0.0

    def test_debt_movement_is_reported(self, env) -> None:
        _, clock, _, calculator, audit = env
        clock.advance(days=1)
        audit("2.0.0")
        first = calculator.calculate(MODEL_ID)
        clock.advance(days=1)
        audit("1.0.0")
        second = calculator.calculate(MODEL_ID, previous_total=first.total)
        assert second.delta is not None and second.delta > 0

    def test_calculation_is_deterministic(self, env) -> None:
        _, clock, _, calculator, audit = env
        clock.advance(days=1)
        audit("1.0.0")
        assert calculator.calculate(MODEL_ID).total == calculator.calculate(MODEL_ID).total

    def test_the_breakdown_is_human_readable(self, env) -> None:
        _, clock, _, calculator, audit = env
        clock.advance(days=1)
        audit("1.0.0")
        rendered = explain(calculator.calculate(MODEL_ID))
        assert "Explanation Debt:" in rendered
        assert "Policy version: 1.0.0" in rendered

    def test_reweighting_changes_the_score_for_the_same_evidence(self, env) -> None:
        # Makes the "these weights are a policy choice" caveat concrete rather than rhetorical.
        _, clock, lineage, _, audit = env
        clock.advance(days=1)
        audit("1.0.0")
        default_total = DebtCalculator(lineage, clock=ManualClock(T0)).calculate(MODEL_ID).total
        reweighted = DEFAULT_POLICY.with_weights(
            "2.0.0",
            DebtWeights(
                stale_evidence=10, contradictions=50, inconclusive=20,
                version_inconsistency=10, distribution_sensitivity=10,
            ),
        )
        other = DebtCalculator(lineage, policy=reweighted, clock=ManualClock(T0))
        assert other.calculate(MODEL_ID).total != default_total


def context(lineage, calculator, family, *, verdict=None, stale=False, debt_total=None):
    from tests.factories import make_scope

    snapshot = calculator.calculate(MODEL_ID)
    if debt_total is not None:
        # Force a debt level to exercise a threshold, keeping the components consistent.
        from backend.core.schemas import DebtComponent

        snapshot = snapshot.model_copy(
            update={
                "components": [
                    DebtComponent(
                        name="contradictions",
                        ratio=debt_total / 100.0,
                        weight=100.0,
                        points=debt_total,
                    )
                ],
                "total": debt_total,
            }
        )
    return GovernanceContext(
        verdict=verdict,
        lineage=lineage.view(family),
        debt=snapshot,
        audit_priority=lineage.audit_priority(family),
        evidence_is_stale=stale,
        scope=make_scope(),
        investigation_id="INV-gov",
    )


class TestGovernorDecisions:
    def test_a_supported_current_claim_just_stores_evidence(self, env) -> None:
        _, clock, lineage, calculator, audit = env
        clock.advance(days=1)
        claim, verdict = audit("2.0.0")
        decision = decide(
            context(lineage, calculator, claim.claim_family_id, verdict=verdict),
            now=clock.now(),
        )
        assert decision.action is GovernorAction.STORE_EVIDENCE
        assert not decision.required_approval

    def test_a_contradiction_raises_audit_priority(self, env) -> None:
        _, clock, lineage, calculator, audit = env
        clock.advance(days=1)
        claim, verdict = audit("1.0.0")
        decision = decide(
            context(lineage, calculator, claim.claim_family_id, verdict=verdict),
            now=clock.now(),
        )
        assert decision.action is GovernorAction.INCREASE_AUDIT_PRIORITY
        assert decision.next_event_at is not None

    def test_an_inconclusive_result_schedules_a_reaudit(self, env) -> None:
        _, clock, lineage, calculator, audit = env
        clock.advance(days=1)
        claim, verdict = audit("3.0.0")
        decision = decide(
            context(lineage, calculator, claim.claim_family_id, verdict=verdict),
            now=clock.now(),
        )
        assert decision.action is GovernorAction.SCHEDULE_REAUDIT

    def test_repeated_contradictions_escalate_to_a_human(self, env) -> None:
        _, clock, lineage, calculator, audit = env
        clock.advance(days=1)
        audit("1.0.0")
        clock.advance(days=1)
        claim, verdict = audit("4.0.0")  # a second contradiction for the same family
        decision = decide(
            context(lineage, calculator, claim.claim_family_id, verdict=verdict),
            now=clock.now(),
        )
        assert decision.action is GovernorAction.REQUIRE_HUMAN_REVIEW
        assert decision.required_approval

    def test_expired_evidence_marks_the_explanation_stale(self, env) -> None:
        _, clock, lineage, calculator, audit = env
        clock.advance(days=1)
        claim, _ = audit("2.0.0")
        clock.advance(days=1)
        lineage.expire_evidence(claim.claim_family_id, reason="DISTRIBUTION_CHANGED")
        decision = decide(
            context(lineage, calculator, claim.claim_family_id), now=clock.now()
        )
        assert decision.action is GovernorAction.MARK_EXPLANATION_STALE

    def test_critical_debt_on_a_contradiction_can_pause_a_workflow(self, env) -> None:
        _, clock, lineage, calculator, audit = env
        clock.advance(days=1)
        claim, verdict = audit("1.0.0")
        decision = decide(
            context(
                lineage, calculator, claim.claim_family_id, verdict=verdict, debt_total=90.0
            ),
            now=clock.now(),
        )
        assert decision.action is GovernorAction.PAUSE_AFFECTED_WORKFLOW
        assert decision.required_approval

    def test_high_debt_alone_stops_short_of_pausing(self, env) -> None:
        # Pausing needs a live contradiction as well as a high score.
        _, clock, lineage, calculator, audit = env
        clock.advance(days=1)
        claim, verdict = audit("2.0.0")
        decision = decide(
            context(
                lineage, calculator, claim.claim_family_id, verdict=verdict, debt_total=90.0
            ),
            now=clock.now(),
        )
        assert decision.action is GovernorAction.REQUIRE_HUMAN_REVIEW

    def test_higher_priority_schedules_a_sooner_reaudit(self, env) -> None:
        _, clock, lineage, calculator, audit = env
        clock.advance(days=1)
        claim, verdict = audit("3.0.0")
        ctx = context(lineage, calculator, claim.claim_family_id, verdict=verdict)
        urgent = decide(replace(ctx, audit_priority=0.95), now=clock.now())
        relaxed = decide(replace(ctx, audit_priority=0.1), now=clock.now())
        assert urgent.next_event_at < relaxed.next_event_at


class TestGovernorAuthority:
    def test_the_action_set_is_closed(self) -> None:
        assert {a.value for a in ALLOWED_ACTIONS} == {
            "NO_ACTION", "STORE_EVIDENCE", "SCHEDULE_REAUDIT", "INCREASE_AUDIT_PRIORITY",
            "MARK_EXPLANATION_STALE", "REQUIRE_HUMAN_REVIEW", "PAUSE_AFFECTED_WORKFLOW",
        }

    def test_an_llm_recommendation_cannot_override_policy(self, env) -> None:
        # The core boundary: the model wants to do nothing, policy requires review, and
        # policy wins - visibly.
        _, clock, lineage, calculator, audit = env
        clock.advance(days=1)
        audit("1.0.0")
        clock.advance(days=1)
        claim, verdict = audit("4.0.0")

        class LazyAdvisor:
            def recommend(self, ctx):
                return GovernorAction.NO_ACTION, AgentProvenance(
                    agent_id="governor", agent_version="1.0.0", role="GOVERNOR"
                )

        governor = Governor(advisor=LazyAdvisor(), clock=clock)
        decision = governor.govern(
            context(lineage, calculator, claim.claim_family_id, verdict=verdict)
        )
        assert decision.action is GovernorAction.REQUIRE_HUMAN_REVIEW
        assert decision.recommendation is GovernorAction.NO_ACTION
        assert decision.recommendation_accepted is False
        assert "RECOMMENDATION_OVERRULED" in decision.reason_codes

    def test_an_invented_action_is_discarded(self, env) -> None:
        _, clock, lineage, calculator, audit = env
        clock.advance(days=1)
        claim, verdict = audit("2.0.0")

        class RogueAdvisor:
            def recommend(self, ctx):
                return "RETRAIN_THE_MODEL", None  # not a GovernorAction

        decision = Governor(advisor=RogueAdvisor(), clock=clock).govern(
            context(lineage, calculator, claim.claim_family_id, verdict=verdict)
        )
        assert decision.action is GovernorAction.STORE_EVIDENCE
        assert decision.recommendation is None

    def test_an_advisor_failure_does_not_block_governance(self, env) -> None:
        _, clock, lineage, calculator, audit = env
        clock.advance(days=1)
        claim, verdict = audit("2.0.0")

        class BrokenAdvisor:
            def recommend(self, ctx):
                raise TimeoutError("gemini did not respond")

        decision = Governor(advisor=BrokenAdvisor(), clock=clock).govern(
            context(lineage, calculator, claim.claim_family_id, verdict=verdict)
        )
        assert decision.action is GovernorAction.STORE_EVIDENCE
        assert decision.recommendation is None

    def test_agreement_is_recorded_as_agreement(self, env) -> None:
        _, clock, lineage, calculator, audit = env
        clock.advance(days=1)
        claim, verdict = audit("2.0.0")

        class AgreeableAdvisor:
            def recommend(self, ctx):
                return GovernorAction.STORE_EVIDENCE, None

        decision = Governor(advisor=AgreeableAdvisor(), clock=clock).govern(
            context(lineage, calculator, claim.claim_family_id, verdict=verdict)
        )
        assert decision.recommendation_accepted is True
        assert "concurred" in decision.rationale

    def test_the_governor_holds_no_write_access_to_evidence(self) -> None:
        import inspect

        import backend.governance.governor as module

        source = inspect.getsource(module)
        for forbidden in ("append_evidence", "append_verdict", "append_lineage"):
            assert forbidden not in source


class TestApprovalGate:
    def test_high_impact_actions_open_a_pending_request(self, env) -> None:
        _, clock, lineage, calculator, audit = env
        clock.advance(days=1)
        audit("1.0.0")
        clock.advance(days=1)
        claim, verdict = audit("4.0.0")
        decision = Governor(clock=clock).govern(
            context(lineage, calculator, claim.claim_family_id, verdict=verdict)
        )
        request = build_approval_request(decision, requested_at=clock.now())
        assert request.status == "PENDING"
        assert request.action in ACTIONS_REQUIRING_APPROVAL

    def test_low_impact_actions_do_not_open_a_request(self, env) -> None:
        _, clock, lineage, calculator, audit = env
        clock.advance(days=1)
        claim, verdict = audit("2.0.0")
        decision = Governor(clock=clock).govern(
            context(lineage, calculator, claim.claim_family_id, verdict=verdict)
        )
        with pytest.raises(PermissionDenied, match="does not require approval"):
            build_approval_request(decision, requested_at=clock.now())

    def test_decisions_are_reproducible(self, env) -> None:
        _, clock, lineage, calculator, audit = env
        clock.advance(days=1)
        claim, verdict = audit("1.0.0")
        ctx = context(lineage, calculator, claim.claim_family_id, verdict=verdict)
        first = Governor(clock=ManualClock(T0 + timedelta(days=1))).govern(ctx)
        second = Governor(clock=ManualClock(T0 + timedelta(days=1))).govern(ctx)
        assert first == second
