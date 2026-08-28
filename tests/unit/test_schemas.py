"""Contract tests for the domain model (prompt 02).

These assert the *negative* space as much as the positive: what the schemas refuse is what
protects the rest of the system, because an invalid object rejected here can never reach
the verifier or the ledger.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.core.enums import (
    ExpectedDirection,
    GovernorAction,
    InterventionType,
    LineageRelation,
    RunKind,
    VerdictStatus,
)
from backend.core.schemas import (
    AgentProvenance,
    ApprovalRequest,
    Claim,
    ConstraintSpec,
    DebtComponent,
    DebtSnapshot,
    Evidence,
    ExperimentPlan,
    ExperimentRun,
    GovernorDecision,
    InterventionSpec,
    LineageEntry,
    RunSummary,
    Verdict,
    VersionScope,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class TestVersionScope:
    def test_valid_scope_round_trips(self, scope: VersionScope) -> None:
        assert scope.label() == "synthetic-triage@1.0.0/baseline_2024.1"
        assert scope.matches(scope.model_copy())

    @pytest.mark.parametrize("bad", ["1.0", "v1.0.0", "1.0.0-rc1", "latest", ""])
    def test_non_semver_model_version_is_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="MAJOR.MINOR.PATCH|string"):
            VersionScope(model_id="m", model_version=bad, distribution_version="d1")

    @pytest.mark.parametrize("bad", ["", "has space", "-leading", "a" * 100])
    def test_malformed_distribution_version_is_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            VersionScope(model_id="m", model_version="1.0.0", distribution_version=bad)

    def test_scopes_differing_in_any_field_do_not_match(self, scope: VersionScope) -> None:
        assert not scope.matches(scope.model_copy(update={"model_version": "2.0.0"}))
        assert not scope.matches(scope.model_copy(update={"distribution_version": "shift_1"}))
        assert not scope.matches(scope.model_copy(update={"model_id": "other"}))


class TestSchemaVersioning:
    def test_incompatible_major_schema_version_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="incompatible schema_version"):
            VersionScope(
                model_id="m",
                model_version="1.0.0",
                distribution_version="d1",
                schema_version="2.0",
            )

    def test_compatible_minor_schema_version_is_accepted(self) -> None:
        scope = VersionScope(
            model_id="m", model_version="1.0.0", distribution_version="d1", schema_version="1.7"
        )
        assert scope.schema_version == "1.7"


class TestImmutability:
    def test_domain_objects_are_frozen(self, claim: Claim) -> None:
        with pytest.raises(ValidationError):
            claim.confidence = 0.1

    def test_unknown_fields_are_rejected(self, scope: VersionScope) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            VersionScope(
                model_id="m",
                model_version="1.0.0",
                distribution_version="d1",
                injected_authority="admin",
            )


class TestClaim:
    def test_valid_claim(self, claim: Claim) -> None:
        assert claim.primacy_claim is True
        assert claim.expected_direction is ExpectedDirection.DECREASE
        assert claim.object_ == "priority_score"

    def test_object_is_addressable_by_its_alias(self, claim: Claim) -> None:
        dumped = claim.model_dump(by_alias=True)
        assert dumped["object"] == "priority_score"

    def test_a_variable_cannot_be_both_intervened_on_and_preserved(self, claim: Claim) -> None:
        payload = claim.model_dump(by_alias=True)
        payload["preserved_constraints"] = ["urgency_marker"]
        with pytest.raises(ValidationError, match="both intervened on and preserved"):
            Claim.model_validate(payload)

    def test_subject_must_be_among_the_target_variables(self, claim: Claim) -> None:
        payload = claim.model_dump(by_alias=True)
        payload["subject"] = "signal_z"
        with pytest.raises(ValidationError, match="must appear in target_variables"):
            Claim.model_validate(payload)

    @pytest.mark.parametrize("field", ["testability_score", "confidence", "audit_priority"])
    @pytest.mark.parametrize("bad", [-0.01, 1.01, 42.0])
    def test_unit_scores_reject_out_of_range_values(
        self, claim: Claim, field: str, bad: float
    ) -> None:
        payload = claim.model_dump(by_alias=True)
        payload[field] = bad
        with pytest.raises(ValidationError):
            Claim.model_validate(payload)

    def test_missing_required_field_is_rejected(self, claim: Claim) -> None:
        payload = claim.model_dump(by_alias=True)
        del payload["source_explanation_hash"]
        with pytest.raises(ValidationError, match="source_explanation_hash"):
            Claim.model_validate(payload)

    def test_invalid_enum_value_is_rejected(self, claim: Claim) -> None:
        payload = claim.model_dump(by_alias=True)
        payload["expected_direction"] = "definitely_causes"
        with pytest.raises(ValidationError):
            Claim.model_validate(payload)

    def test_inverted_validity_window_is_rejected(self, claim: Claim) -> None:
        payload = claim.model_dump(by_alias=True)
        payload["valid_until"] = (T0 - timedelta(days=1)).isoformat()
        with pytest.raises(ValidationError, match="valid_until must be after valid_from"):
            Claim.model_validate(payload)

    def test_quarantine_must_state_a_reason(self, claim: Claim) -> None:
        payload = claim.model_dump(by_alias=True)
        payload["quarantined"] = True
        with pytest.raises(ValidationError, match="must record why"):
            Claim.model_validate(payload)

    def test_validity_window_is_honoured(self, claim: Claim) -> None:
        bounded = claim.model_copy(update={"valid_until": T0 + timedelta(days=30)})
        assert bounded.is_valid_at(T0)
        assert bounded.is_valid_at(T0 + timedelta(days=29))
        assert not bounded.is_valid_at(T0 + timedelta(days=31))
        assert not bounded.is_valid_at(T0 - timedelta(seconds=1))

    def test_duplicate_target_variables_are_rejected(self, claim: Claim) -> None:
        payload = claim.model_dump(by_alias=True)
        payload["target_variables"] = ["urgency_marker", "urgency_marker"]
        with pytest.raises(ValidationError, match="unique"):
            Claim.model_validate(payload)


class TestInterventionSpec:
    def test_neutralize_requires_a_value(self) -> None:
        with pytest.raises(ValidationError, match="requires an explicit target value"):
            InterventionSpec(variable="x", intervention_type=InterventionType.NEUTRALIZE)

    def test_increase_requires_a_positive_delta(self) -> None:
        with pytest.raises(ValidationError, match="positive delta"):
            InterventionSpec(
                variable="x", intervention_type=InterventionType.INCREASE, delta=-0.1
            )

    def test_decrease_requires_a_negative_delta(self) -> None:
        with pytest.raises(ValidationError, match="negative delta"):
            InterventionSpec(
                variable="x", intervention_type=InterventionType.DECREASE, delta=0.1
            )

    def test_ablation_needs_no_parameters(self) -> None:
        spec = InterventionSpec(variable="x", intervention_type=InterventionType.ABLATION)
        assert spec.value is None


class TestExperimentPlan:
    def test_valid_plan(self, plan: ExperimentPlan) -> None:
        assert plan.repetitions == 12
        assert plan.control is not None

    def test_control_must_target_a_different_variable(self, plan: ExperimentPlan) -> None:
        payload = plan.model_dump(by_alias=True)
        payload["control"]["variable"] = payload["intervention"]["variable"]
        with pytest.raises(ValidationError, match="different variable"):
            ExperimentPlan.model_validate(payload)

    def test_intervened_variable_cannot_also_be_preserved(self, plan: ExperimentPlan) -> None:
        payload = plan.model_dump(by_alias=True)
        payload["constraints"]["preserved_features"].append("urgency_marker")
        with pytest.raises(ValidationError, match="both intervened on and preserved"):
            ExperimentPlan.model_validate(payload)

    def test_repetitions_below_the_plans_own_minimum_are_rejected(
        self, plan: ExperimentPlan
    ) -> None:
        payload = plan.model_dump(by_alias=True)
        payload["repetitions"] = 2
        with pytest.raises(ValidationError, match="below the plan's own minimum"):
            ExperimentPlan.model_validate(payload)

    def test_repetitions_out_of_hard_bounds_are_rejected(self, plan: ExperimentPlan) -> None:
        for bad in (0, 101):
            payload = plan.model_dump(by_alias=True)
            payload["repetitions"] = bad
            with pytest.raises(ValidationError):
                ExperimentPlan.model_validate(payload)

    def test_inverted_feature_bounds_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="inverted"):
            ConstraintSpec(feature_bounds={"x": (1.0, 0.0)})


class TestRunAndEvidence:
    def _summary(self, kind: RunKind, scores: list[float]) -> RunSummary:
        return RunSummary(
            kind=kind,
            n=len(scores),
            mean=sum(scores) / len(scores),
            stdev=0.0,
            minimum=min(scores),
            maximum=max(scores),
            scores=scores,
            run_ids=[f"RUN-{kind}-{i}" for i in range(len(scores))],
        )

    def test_run_rejects_a_non_finite_feature(self, scope: VersionScope) -> None:
        with pytest.raises(ValidationError, match="not finite"):
            ExperimentRun(
                id="RUN-1",
                experiment_id="EXP-1",
                kind=RunKind.BASELINE,
                index=0,
                scope=scope,
                features={"urgency_marker": float("nan")},
                score=0.5,
                decision="HIGH_PRIORITY",
                model_explanation="x",
                input_hash="sha256:a",
                output_hash="sha256:b",
                executed_at=T0,
                duration_ms=1.0,
            )

    def test_run_requires_a_feature_vector(self, scope: VersionScope) -> None:
        with pytest.raises(ValidationError, match="must record the feature vector"):
            ExperimentRun(
                id="RUN-1",
                experiment_id="EXP-1",
                kind=RunKind.BASELINE,
                index=0,
                scope=scope,
                features={},
                score=0.5,
                decision="HIGH",
                model_explanation="x",
                input_hash="sha256:a",
                output_hash="sha256:b",
                executed_at=T0,
                duration_ms=1.0,
            )

    def test_summary_rejects_a_mismatched_count(self) -> None:
        with pytest.raises(ValidationError, match="does not match"):
            RunSummary(
                kind=RunKind.BASELINE, n=5, mean=0.5, stdev=0.0,
                minimum=0.5, maximum=0.5, scores=[0.5],
            )

    def test_summary_rejects_a_mean_outside_its_range(self) -> None:
        with pytest.raises(ValidationError, match="outside the observed range"):
            RunSummary(
                kind=RunKind.BASELINE, n=2, mean=9.0, stdev=0.0,
                minimum=0.4, maximum=0.6, scores=[0.4, 0.6],
            )

    def _evidence(self, scope: VersionScope, **overrides: object) -> Evidence:
        base = {
            "id": "EVD-1",
            "experiment_id": "EXP-1",
            "claim_id": "CLM-1",
            "claim_family_id": "FAM-1",
            "scope": scope,
            "protocol_version": "1.0.0",
            "baseline": self._summary(RunKind.BASELINE, [0.7, 0.7, 0.7]),
            "intervention": self._summary(RunKind.INTERVENTION, [0.6, 0.6, 0.6]),
            "effect_size": -0.1,
            "reproducibility": 1.0,
            "validity_score": 1.0,
            "instability": 0.0,
            "run_ids": ["RUN-1"],
            "input_hashes": ["sha256:a"],
            "output_hashes": ["sha256:b"],
            "evidence_hash": "sha256:c",
            "created_at": T0,
        }
        base.update(overrides)
        return Evidence(**base)  # type: ignore[arg-type]

    def test_valid_evidence(self, scope: VersionScope) -> None:
        assert self._evidence(scope).effect_size == pytest.approx(-0.1)

    def test_evidence_rejects_unequal_paired_arms(self, scope: VersionScope) -> None:
        with pytest.raises(ValidationError, match="equal arms"):
            self._evidence(
                scope, intervention=self._summary(RunKind.INTERVENTION, [0.6, 0.6])
            )

    def test_evidence_rejects_a_mislabelled_arm(self, scope: VersionScope) -> None:
        with pytest.raises(ValidationError, match="must be of kind BASELINE"):
            self._evidence(scope, baseline=self._summary(RunKind.CONTROL, [0.7, 0.7, 0.7]))

    def test_evidence_rejects_an_inverted_confidence_interval(self, scope: VersionScope) -> None:
        with pytest.raises(ValidationError, match="inverted"):
            self._evidence(scope, effect_ci=(0.2, -0.2))

    def test_evidence_requires_provenance_hashes(self, scope: VersionScope) -> None:
        for field in ("run_ids", "input_hashes", "output_hashes"):
            with pytest.raises(ValidationError):
                self._evidence(scope, **{field: []})

    def test_evidence_carries_no_verdict_field(self, scope: VersionScope) -> None:
        # Measurement and conclusion are separate objects on purpose.
        assert "status" not in Evidence.model_fields
        assert "verdict" not in Evidence.model_fields


class TestVerdict:
    def _verdict(self, scope: VersionScope, **overrides: object) -> Verdict:
        base = {
            "id": "VDT-1",
            "claim_id": "CLM-1",
            "claim_family_id": "FAM-1",
            "scope": scope,
            "protocol_version": "1.0.0",
            "status": VerdictStatus.CONTRADICTED,
            "behavioral_support": 0.2,
            "intervention_validity": 0.97,
            "reproducibility": 0.9,
            "contradiction_score": 0.8,
            "effect_size": -0.02,
            "expected_direction": ExpectedDirection.DECREASE,
            "observed_direction": ExpectedDirection.NO_CHANGE,
            "evidence_ids": ["EVD-1"],
            "reason_codes": ["EFFECT_BELOW_THRESHOLD"],
            "rationale": "effect below threshold",
            "created_at": T0,
        }
        base.update(overrides)
        return Verdict(**base)  # type: ignore[arg-type]

    def test_a_verdict_must_reference_evidence(self, scope: VersionScope) -> None:
        with pytest.raises(ValidationError):
            self._verdict(scope, evidence_ids=[])

    def test_a_verdict_must_carry_reason_codes(self, scope: VersionScope) -> None:
        with pytest.raises(ValidationError):
            self._verdict(scope, reason_codes=[])

    def test_only_the_three_verdicts_exist(self, scope: VersionScope) -> None:
        assert {s.value for s in VerdictStatus} == {
            "SUPPORTED",
            "CONTRADICTED",
            "INCONCLUSIVE",
        }
        with pytest.raises(ValidationError):
            self._verdict(scope, status="PROBABLY_TRUE")

    def test_verifier_version_must_be_semver(self, scope: VersionScope) -> None:
        with pytest.raises(ValidationError, match="MAJOR.MINOR.PATCH"):
            self._verdict(scope, verifier_version="latest")


class TestLineageEntry:
    def _entry(self, scope: VersionScope, **overrides: object) -> LineageEntry:
        base = {
            "id": "LIN-1",
            "claim_family_id": "FAM-1",
            "claim_id": "CLM-1",
            "scope": scope,
            "protocol_version": "1.0.0",
            "verdict_id": "VDT-1",
            "status": VerdictStatus.CONTRADICTED,
            "evidence_ids": ["EVD-1"],
            "behavioral_support": 0.2,
            "intervention_validity": 0.97,
            "reproducibility": 0.9,
            "effect_size": -0.02,
            "relation": LineageRelation.INITIAL,
            "valid_from": T0,
            "created_at": T0,
            "input_hashes": ["sha256:a"],
            "output_hashes": ["sha256:b"],
            "verifier_version": "1.0.0",
            "entry_hash": "sha256:e",
        }
        base.update(overrides)
        return LineageEntry(**base)  # type: ignore[arg-type]

    def test_initial_entry_cannot_supersede(self, scope: VersionScope) -> None:
        with pytest.raises(ValidationError, match="cannot supersede"):
            self._entry(scope, supersedes_entry_id="LIN-0")

    @pytest.mark.parametrize(
        "relation", [LineageRelation.SUPERSEDES, LineageRelation.DISPUTES]
    )
    def test_superseding_entry_must_name_its_target(
        self, scope: VersionScope, relation: LineageRelation
    ) -> None:
        with pytest.raises(ValidationError, match="must name the entry"):
            self._entry(scope, relation=relation)

    def test_expiry_requires_a_recorded_reason(self, scope: VersionScope) -> None:
        with pytest.raises(ValidationError, match="requires a recorded reason"):
            self._entry(scope, valid_until=T0 + timedelta(days=1))

    def test_temporal_currency(self, scope: VersionScope) -> None:
        entry = self._entry(
            scope,
            valid_until=T0 + timedelta(days=10),
            expired_reason="DISTRIBUTION_CHANGED",
        )
        assert entry.is_current_at(T0 + timedelta(days=5))
        assert not entry.is_current_at(T0 + timedelta(days=11))
        assert not entry.is_current_at(T0 - timedelta(days=1))

    def test_open_ended_entry_stays_current(self, scope: VersionScope) -> None:
        assert self._entry(scope).is_current_at(T0 + timedelta(days=3650))


class TestDebt:
    def test_component_arithmetic_must_be_self_consistent(self) -> None:
        with pytest.raises(ValidationError, match="not self-consistent"):
            DebtComponent(name="contradictions", ratio=0.5, weight=25.0, points=99.0)

    def test_snapshot_total_must_equal_its_components(self) -> None:
        components = [
            DebtComponent(name="a", ratio=0.5, weight=25.0, points=12.5),
            DebtComponent(name="b", ratio=0.2, weight=20.0, points=4.0),
        ]
        DebtSnapshot(
            id="DBT-1", model_id="m", scope_label="m@1.0.0/d", policy_version="1.0.0",
            components=components, total=16.5, computed_at=T0,
        )
        with pytest.raises(ValidationError, match="does not equal the sum"):
            DebtSnapshot(
                id="DBT-2", model_id="m", scope_label="m@1.0.0/d", policy_version="1.0.0",
                components=components, total=73.0, computed_at=T0,
            )

    def test_delta_reports_movement(self) -> None:
        snapshot = DebtSnapshot(
            id="DBT-1", model_id="m", scope_label="m@1.0.0/d", policy_version="1.0.0",
            components=[DebtComponent(name="a", ratio=0.5, weight=25.0, points=12.5)],
            total=12.5, previous_total=4.0, computed_at=T0,
        )
        assert snapshot.delta == pytest.approx(8.5)


class TestGovernance:
    def _decision(self, scope: VersionScope, **overrides: object) -> GovernorDecision:
        base = {
            "id": "GOV-1",
            "investigation_id": "INV-1",
            "claim_family_id": "FAM-1",
            "scope": scope,
            "action": GovernorAction.REQUIRE_HUMAN_REVIEW,
            "reason_codes": ["HIGH_DEBT"],
            "rationale": "debt above threshold",
            "policy_version": "1.0.0",
            "created_at": T0,
        }
        base.update(overrides)
        return GovernorDecision(**base)  # type: ignore[arg-type]

    def test_an_llm_recommendation_that_was_overruled_must_say_so(
        self, scope: VersionScope
    ) -> None:
        with pytest.raises(ValidationError, match="recommendation_accepted must reflect"):
            self._decision(
                scope,
                recommendation=GovernorAction.NO_ACTION,
                recommendation_accepted=True,
            )

    def test_overruling_is_recorded_honestly(self, scope: VersionScope) -> None:
        decision = self._decision(
            scope,
            recommendation=GovernorAction.NO_ACTION,
            recommendation_accepted=False,
        )
        assert decision.action is GovernorAction.REQUIRE_HUMAN_REVIEW
        assert decision.recommendation is GovernorAction.NO_ACTION

    def test_actions_outside_the_allowed_set_are_rejected(self, scope: VersionScope) -> None:
        with pytest.raises(ValidationError):
            self._decision(scope, action="RETRAIN_MODEL")

    def test_a_resolved_approval_must_name_its_decider(self) -> None:
        with pytest.raises(ValidationError, match="must record who decided"):
            ApprovalRequest(
                id="APR-1", decision_id="GOV-1", investigation_id="INV-1",
                action=GovernorAction.PAUSE_AFFECTED_WORKFLOW,
                justification="debt spike", status="APPROVED", requested_at=T0,
            )

    def test_a_pending_approval_needs_no_decider(self) -> None:
        request = ApprovalRequest(
            id="APR-1", decision_id="GOV-1", investigation_id="INV-1",
            action=GovernorAction.PAUSE_AFFECTED_WORKFLOW,
            justification="debt spike", requested_at=T0,
        )
        assert request.status == "PENDING"


class TestProvenance:
    def test_agent_version_must_be_semver(self) -> None:
        with pytest.raises(ValidationError, match="MAJOR.MINOR.PATCH"):
            AgentProvenance(agent_id="a", agent_version="dev", role="INVESTIGATOR")

    def test_attempts_are_bounded(self) -> None:
        with pytest.raises(ValidationError):
            AgentProvenance(
                agent_id="a", agent_version="1.0.0", role="INVESTIGATOR", attempts=99
            )
