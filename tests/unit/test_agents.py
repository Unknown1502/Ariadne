"""Agent behaviour and fleet boundaries (prompts 04, 05, 10).

The recurring theme: an agent's *output* is untrusted and validated, and an agent's
*authority* is declared and enforced. Neither is left to good behaviour.
"""

from __future__ import annotations

import pytest

from backend.agents.experimenter import Experimenter
from backend.agents.investigator import Investigator
from backend.agents.llm import LLMRequest, OfflineReasoner, extract_json
from backend.agents.registry import (
    DEFAULT_MANIFESTS,
    AgentRegistry,
    assert_four_roles,
)
from backend.core.agent_contracts import AgentManifest, ToolPermission
from backend.core.clock import ManualClock
from backend.core.enums import AgentRole, ExpectedDirection, RiskLevel, VerdictStatus
from backend.core.errors import (
    AgentOutputError,
    LoopBudgetExceeded,
    PermissionDenied,
    UntestableExplanation,
    ValidationError,
)
from backend.core.schemas import VersionScope
from tests.factories import T0, make_scope


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def investigator(clock: ManualClock) -> Investigator:
    return Investigator(OfflineReasoner(), clock=clock)


@pytest.fixture
def experimenter(clock: ManualClock) -> Experimenter:
    from backend.experiment_engine.runner import ExperimentRunner

    return Experimenter(OfflineReasoner(), runner=ExperimentRunner(clock=clock), clock=clock)


def compile_it(investigator: Investigator, explanation: str, scope: VersionScope | None = None):
    return investigator.compile_claim(
        explanation=explanation,
        decision="HIGH_PRIORITY",
        scope=scope or make_scope(),
        investigation_id="INV-test",
    )


class TestJsonExtraction:
    def test_plain_json_parses(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json_parses(self) -> None:
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_surrounded_by_prose_parses(self) -> None:
        assert extract_json('Sure! Here you go:\n{"a": 1}\nHope that helps.') == {"a": 1}

    def test_empty_response_is_refused(self) -> None:
        with pytest.raises(AgentOutputError, match="empty response"):
            extract_json("   ")

    def test_malformed_json_is_refused_rather_than_guessed(self) -> None:
        with pytest.raises(AgentOutputError, match="malformed JSON"):
            extract_json('{"a": 1,,,}')

    def test_a_json_array_is_not_an_object(self) -> None:
        with pytest.raises(AgentOutputError, match="no JSON object"):
            extract_json("[1, 2, 3]")

    def test_a_top_level_non_object_inside_prose_is_refused(self) -> None:
        with pytest.raises(AgentOutputError):
            extract_json('the answer is "yes"')

    def test_prose_with_no_json_is_refused(self) -> None:
        with pytest.raises(AgentOutputError, match="no JSON object"):
            extract_json("I am unable to help with that request.")


class TestInvestigator:
    def test_a_faithful_explanation_compiles_to_a_primacy_claim(
        self, investigator: Investigator
    ) -> None:
        claim, _ = compile_it(investigator, "Urgency marker was the primary driver.")
        assert claim.subject == "urgency_marker"
        assert claim.primacy_claim is True
        assert claim.expected_direction is ExpectedDirection.DECREASE
        assert claim.testability_score > 0.8

    def test_an_influence_claim_is_not_a_primacy_claim(
        self, investigator: Investigator
    ) -> None:
        # The distinction that decides whether a control can refute the claim.
        claim, _ = compile_it(investigator, "The urgency marker contributed to this score.")
        assert claim.primacy_claim is False

    def test_a_vague_explanation_is_reported_as_untestable(
        self, investigator: Investigator
    ) -> None:
        # "Several factors contributed" states no hypothesis. Saying so is the correct
        # output; inventing a driver to test would be worse than returning nothing.
        with pytest.raises(UntestableExplanation, match="no testable driver"):
            compile_it(
                investigator, "Several factors and the overall complex picture contributed."
            )

    def test_an_untestable_explanation_is_not_retried(
        self, clock: ManualClock
    ) -> None:
        # Retrying would only pressure the model into naming a driver nobody claimed.
        reasoner = OfflineReasoner()
        agent = Investigator(reasoner, clock=clock)
        with pytest.raises(UntestableExplanation):
            compile_it(agent, "Multiple factors contributed to this decision.")
        assert reasoner._calls == 1

    def test_the_claim_binds_to_its_model_version(self, investigator: Investigator) -> None:
        claim, _ = compile_it(investigator, "Urgency marker was the primary driver.",
                              make_scope("2.0.0"))
        assert claim.scope.model_version == "2.0.0"

    def test_the_same_claim_across_versions_shares_a_family(
        self, investigator: Investigator
    ) -> None:
        # This is what makes lineage possible at all.
        first, _ = compile_it(investigator, "Urgency marker was the primary driver.",
                              make_scope("1.0.0"))
        second, _ = compile_it(investigator, "Urgency marker was the primary driver.",
                               make_scope("2.0.0"))
        assert first.claim_family_id == second.claim_family_id
        assert first.id != second.id

    def test_provenance_records_the_reasoner_honestly(
        self, investigator: Investigator
    ) -> None:
        # The offline reasoner must never be presented as Gemini.
        claim, _ = compile_it(investigator, "Urgency marker was the primary driver.")
        assert claim.provenance.llm_model == "offline-deterministic-reasoner/1.0.0"
        assert claim.provenance.prompt_version == "claim-compiler/1.0.0"
        assert investigator.uses_language_model is False

    def test_an_empty_explanation_is_refused(self, investigator: Investigator) -> None:
        with pytest.raises(ValidationError):
            compile_it(investigator, "   ")

    def test_a_hallucinated_feature_is_retried_then_fails_loudly(
        self, clock: ManualClock
    ) -> None:
        class Hallucinating(OfflineReasoner):
            def generate(self, request):
                import json

                from backend.agents.llm import LLMResponse

                return LLMResponse(
                    text=json.dumps({
                        "subject": "patient_wealth",  # not a laboratory feature
                        "predicate": "is_primary_driver", "object": "priority_score",
                        "expected_direction": "decrease", "target_variables": ["patient_wealth"],
                        "testability_score": 0.9, "confidence": 0.9,
                    }),
                    model="test",
                )

        with pytest.raises(LoopBudgetExceeded, match="within 3 attempts"):
            compile_it(Investigator(Hallucinating(), clock=clock), "Wealth drove this.")

    def test_a_malformed_response_is_retried_within_budget(self, clock: ManualClock) -> None:
        agent = Investigator(OfflineReasoner(fail_times=2), clock=clock)
        claim, outcome = compile_it(agent, "Urgency marker was the primary driver.")
        assert outcome.attempts == 3
        assert claim.provenance.attempts == 3
        assert [i.succeeded for i in outcome.invocations] == [False, False, True]

    def test_exceeding_the_loop_budget_raises(self, clock: ManualClock) -> None:
        agent = Investigator(OfflineReasoner(fail_times=99), clock=clock)
        with pytest.raises(LoopBudgetExceeded):
            compile_it(agent, "Urgency marker was the primary driver.")

    def test_a_timeout_is_retried_then_surfaces(self, clock: ManualClock) -> None:
        agent = Investigator(OfflineReasoner(hang=True), clock=clock)
        with pytest.raises(LoopBudgetExceeded, match="AgentTimeout|timeout"):
            compile_it(agent, "Urgency marker was the primary driver.")

    def test_every_attempt_is_recorded(self, clock: ManualClock) -> None:
        # An agent that failed silently would look identical to one never called.
        agent = Investigator(OfflineReasoner(fail_times=1), clock=clock)
        _, outcome = compile_it(agent, "Urgency marker was the primary driver.")
        assert len(outcome.invocations) == 2
        assert outcome.invocations[0].error_code
        assert outcome.invocations[1].output_hash


class TestExperimenter:
    def test_a_probe_targets_the_claim_subject(
        self, investigator: Investigator, experimenter: Experimenter
    ) -> None:
        claim, _ = compile_it(investigator, "Urgency marker was the primary driver.")
        plan, _ = experimenter.plan_experiment(claim)
        assert plan.intervention.variable == "urgency_marker"
        assert plan.claim_id == claim.id

    def test_a_primacy_claim_gets_a_control_on_another_variable(
        self, investigator: Investigator, experimenter: Experimenter
    ) -> None:
        claim, _ = compile_it(investigator, "Urgency marker was the primary driver.")
        plan, _ = experimenter.plan_experiment(claim)
        assert plan.control is not None
        assert plan.control.variable != plan.intervention.variable

    def test_neutralization_uses_the_declared_neutral_value(
        self, investigator: Investigator, clock: ManualClock
    ) -> None:
        # An agent must not be able to redefine what neutralizing means and prove nothing
        # while looking rigorous.
        class SneakyPlanner(OfflineReasoner):
            def _plan_experiment(self, context):
                payload = super()._plan_experiment(context)
                payload["intervention_value"] = 0.94  # barely moves a high-urgency case
                return payload

        from backend.experiment_engine.runner import ExperimentRunner

        agent = Experimenter(SneakyPlanner(), runner=ExperimentRunner(clock=clock), clock=clock)
        claim, _ = compile_it(investigator, "Urgency marker was the primary driver.")
        plan, _ = agent.plan_experiment(claim)
        assert plan.intervention.value == 0.5

    def test_preserved_features_are_computed_not_taken_from_the_model(
        self, investigator: Investigator, clock: ManualClock
    ) -> None:
        class LazyPlanner(OfflineReasoner):
            def _plan_experiment(self, context):
                payload = super()._plan_experiment(context)
                payload["preserved_features"] = []  # try to drop every constraint
                return payload

        from backend.experiment_engine.runner import ExperimentRunner

        agent = Experimenter(LazyPlanner(), runner=ExperimentRunner(clock=clock), clock=clock)
        claim, _ = compile_it(investigator, "Urgency marker was the primary driver.")
        plan, _ = agent.plan_experiment(claim)
        assert plan.constraints.preserved_features == ["signal_b"]

    def test_a_probe_on_the_wrong_variable_is_rejected(
        self, investigator: Investigator, clock: ManualClock
    ) -> None:
        class WrongTarget(OfflineReasoner):
            def _plan_experiment(self, context):
                payload = super()._plan_experiment(context)
                payload["target_variable"] = "signal_b"
                return payload

        from backend.experiment_engine.runner import ExperimentRunner

        agent = Experimenter(WrongTarget(), runner=ExperimentRunner(clock=clock), clock=clock)
        claim, _ = compile_it(investigator, "Urgency marker was the primary driver.")
        with pytest.raises(LoopBudgetExceeded):
            agent.plan_experiment(claim)

    def test_a_primacy_claim_without_a_control_is_rejected(
        self, investigator: Investigator, clock: ManualClock
    ) -> None:
        class NoControl(OfflineReasoner):
            def _plan_experiment(self, context):
                payload = super()._plan_experiment(context)
                payload["control_variable"] = None
                return payload

        from backend.experiment_engine.runner import ExperimentRunner

        agent = Experimenter(NoControl(), runner=ExperimentRunner(clock=clock), clock=clock)
        claim, _ = compile_it(investigator, "Urgency marker was the primary driver.")
        with pytest.raises(LoopBudgetExceeded):
            agent.plan_experiment(claim)

    def test_the_experimenter_cannot_express_a_verdict(self) -> None:
        import inspect

        import backend.agents.experimenter as module

        source = inspect.getsource(module)
        for word in ("SUPPORTED", "CONTRADICTED", "generate_verdict"):
            assert word not in source, f"the Experimenter references {word}"

    def test_the_experimenter_never_reads_the_models_own_explanation(self) -> None:
        # Inferring success from the model's self-description would be circular.
        import inspect

        import backend.agents.experimenter as module

        assert "model_explanation" not in inspect.getsource(module)


class TestRegistry:
    def test_exactly_four_cognitive_roles(self) -> None:
        registry = AgentRegistry.with_defaults()
        assert_four_roles(registry)
        assert len(registry.manifests) == 4

    def test_a_fifth_agent_is_rejected(self) -> None:
        registry = AgentRegistry.with_defaults()
        registry.register(
            AgentManifest(
                agent_id="summarizer", version="1.0.0", role=AgentRole.INVESTIGATOR,
                owner="x", capabilities=["summarize"], input_schema="Claim",
                output_schema="str",
            )
        )
        with pytest.raises(PermissionDenied, match="exactly"):
            assert_four_roles(registry)

    def test_the_verifier_may_not_use_a_language_model(self) -> None:
        with pytest.raises(Exception, match="must not use an LLM"):
            AgentManifest(
                agent_id="verifier", version="1.0.0", role=AgentRole.VERIFIER, owner="x",
                capabilities=["verify"], input_schema="Evidence", output_schema="Verdict",
                uses_llm=True,
            )

    def test_no_reasoning_agent_can_write_verdicts(self) -> None:
        for manifest in DEFAULT_MANIFESTS:
            if manifest.role is not AgentRole.VERIFIER:
                assert "verdict" not in manifest.write_scopes, manifest.agent_id

    def test_the_investigator_cannot_write_evidence(self) -> None:
        from backend.agents.registry import INVESTIGATOR_MANIFEST

        assert INVESTIGATOR_MANIFEST.write_scopes == ["claim"]

    def test_the_governor_cannot_write_evidence_or_verdicts(self) -> None:
        with pytest.raises(Exception, match="must not write evidence or verdicts"):
            AgentManifest(
                agent_id="governor", version="1.0.0", role=AgentRole.GOVERNOR, owner="x",
                capabilities=["govern"], input_schema="X", output_schema="Y",
                write_scopes=["verdict"],
            )

    def test_routing_requires_both_capability_and_schema(self) -> None:
        registry = AgentRegistry.with_defaults()
        assert registry.route("compile_claim", "ExplanationReceivedPayload").agent_id == (
            "investigator"
        )
        with pytest.raises(PermissionDenied, match="carries"):
            registry.route("compile_claim", "SomethingElse")

    def test_routing_an_unknown_capability_fails(self) -> None:
        with pytest.raises(PermissionDenied, match="no registered agent declares"):
            AgentRegistry.with_defaults().route("delete_everything", "Claim")

    def test_a_quarantined_agent_is_not_routed_to(self) -> None:
        registry = AgentRegistry.with_defaults()
        registry.quarantine("investigator", "REPEATED_MALFORMED_OUTPUT", "3 bad responses")
        with pytest.raises(PermissionDenied, match="quarantined"):
            registry.route("compile_claim", "ExplanationReceivedPayload")

    def test_failures_accumulate_before_quarantine(self) -> None:
        registry = AgentRegistry.with_defaults()
        assert registry.record_failure("investigator", "BAD_OUTPUT", "x") is False
        assert registry.record_failure("investigator", "BAD_OUTPUT", "x") is False
        assert registry.record_failure("investigator", "BAD_OUTPUT", "x") is True
        assert registry.is_quarantined("investigator")

    def test_a_quarantined_agent_can_be_released(self) -> None:
        registry = AgentRegistry.with_defaults()
        registry.quarantine("investigator", "X", "y")
        registry.release("investigator")
        assert not registry.is_quarantined("investigator")

    def test_a_manifest_cannot_grant_a_tool_above_its_own_ceiling(self) -> None:
        with pytest.raises(Exception, match="capped at"):
            AgentManifest(
                agent_id="a", version="1.0.0", role=AgentRole.INVESTIGATOR, owner="x",
                capabilities=["c"], input_schema="I", output_schema="O",
                allowed_tools=[ToolPermission(tool="danger", max_risk=RiskLevel.HIGH)],
                max_risk_level=RiskLevel.LOW,
            )


class TestToolPermissions:
    def test_an_ungranted_tool_is_denied(self, investigator: Investigator) -> None:
        with pytest.raises(PermissionDenied, match="no grant for tool"):
            investigator.check_tool(
                "database.execute_sql", risk=RiskLevel.HIGH, investigation_id="INV-1"
            )

    def test_a_granted_tool_is_allowed_and_recorded(self, investigator: Investigator) -> None:
        call = investigator.check_tool(
            "lineage.get_family", risk=RiskLevel.READ_ONLY, investigation_id="INV-1"
        )
        assert call.allowed is True
        assert call.arguments_hash.startswith("sha256:")

    def test_exceeding_a_tools_risk_ceiling_is_denied(
        self, investigator: Investigator
    ) -> None:
        with pytest.raises(PermissionDenied, match="capped at"):
            investigator.check_tool(
                "lineage.get_family", risk=RiskLevel.HIGH, investigation_id="INV-1"
            )

    def test_writing_an_ungranted_scope_is_denied(self, investigator: Investigator) -> None:
        with pytest.raises(PermissionDenied, match="no write scope"):
            investigator.require_write("verdict")

    def test_the_investigator_may_write_claims(self, investigator: Investigator) -> None:
        investigator.require_write("claim")  # does not raise


class TestOfflineReasonerHonesty:
    def test_it_does_not_claim_to_be_a_language_model(self) -> None:
        reasoner = OfflineReasoner()
        assert reasoner.is_language_model is False
        assert "offline" in reasoner.model_name

    def test_an_unknown_task_is_refused_rather_than_improvised(self) -> None:
        with pytest.raises(AgentOutputError, match="no rule for task"):
            OfflineReasoner().generate(
                LLMRequest(system="", user="", task="write_me_a_poem")
            )

    def test_it_recommends_review_when_debt_is_high(self) -> None:
        response = OfflineReasoner().generate(
            LLMRequest(
                system="", user="", task="recommend_action",
                context={"debt_total": 80.0, "verdict_status": "CONTRADICTED",
                         "contradiction_count": 2},
            )
        )
        assert "REQUIRE_HUMAN_REVIEW" in response.text

    def test_verdict_statuses_are_never_produced_by_the_reasoner(self) -> None:
        # The offline reasoner has no path that emits a verdict.
        response = OfflineReasoner().generate(
            LLMRequest(system="", user="", task="compile_claim",
                       context={"explanation": "Urgency marker was the primary driver."})
        )
        assert not any(s.value in response.text for s in VerdictStatus)
