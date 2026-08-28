"""The Experimenter (prompt 05).

Designs the probe, then hands it to deterministic code to execute. The split is the point:
Gemini decides *what to test* - which control best challenges a primacy claim, what could
confound the result - and Python decides *what happened*.

The Experimenter never sees a verdict and has no vocabulary for one. It cannot infer success
from the target model's own explanation string either; the runner records that string as
data, and nothing in this file reads it. An explanation that says "urgency drove this" is
the thing under test, not evidence about itself.

Its plans are proposals. Every one is validated before execution - target variable must be
one the claim named, control must differ, constraints must be preservable - and a plan that
fails validation produces EXPERIMENT_REJECTED rather than a quietly degraded experiment.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from backend.agents.audit import AuditSink
from backend.agents.base import AgentBase, ReasoningOutcome
from backend.agents.llm import LLMClient, LLMRequest
from backend.agents.prompts import PROBE_DESIGNER_SYSTEM, build_probe_prompt
from backend.agents.registry import EXPERIMENTER_MANIFEST
from backend.agents.sanitizer import sanitize_identifier
from backend.core.agent_contracts import AgentManifest
from backend.core.clock import Clock
from backend.core.enums import ExpectedDirection, InterventionType, RiskLevel
from backend.core.errors import InterventionRejected
from backend.core.ids import experiment_id
from backend.core.schemas import (
    Claim,
    ConstraintSpec,
    ExperimentPlan,
    InterventionSpec,
)
from backend.core.versions import PROTOCOL_VERSION
from backend.experiment_engine.distributions import (
    FEATURE_INDEX,
    FEATURE_NAMES,
    fixture_set_for,
    neutral_value,
)
from backend.experiment_engine.runner import ExperimentResult, ExperimentRunner

VALID_INTERVENTIONS = {t.value for t in InterventionType}


class Experimenter(AgentBase):
    """Designs and executes a constrained probe."""

    def __init__(
        self,
        llm: LLMClient,
        *,
        manifest: AgentManifest = EXPERIMENTER_MANIFEST,
        runner: ExperimentRunner | None = None,
        clock: Clock | None = None,
        default_repetitions: int = 24,
        default_seed: int = 20260101,
        audit: AuditSink | None = None,
    ) -> None:
        super().__init__(manifest, llm, clock=clock, audit=audit)
        self._runner = runner or ExperimentRunner(clock=clock)
        self._default_repetitions = default_repetitions
        self._default_seed = default_seed

    def plan_experiment(
        self, claim: Claim, *, created_at: datetime | None = None
    ) -> tuple[ExperimentPlan, ReasoningOutcome]:
        """Design a probe of the claim."""
        self.require_write("experiment")
        moment = created_at or self._clock.now()

        request = LLMRequest(
            system=PROBE_DESIGNER_SYSTEM,
            user=build_probe_prompt(
                subject=claim.subject,
                predicate=claim.predicate,
                expected_direction=str(claim.expected_direction),
                primacy_claim=claim.primacy_claim,
                available_features=list(FEATURE_NAMES),
                neutral_values={name: neutral_value(name) for name in FEATURE_NAMES},
                model_version=claim.scope.model_version,
                distribution_version=claim.scope.distribution_version,
                default_repetitions=self._default_repetitions,
                prior_verdict=str(claim.prior_verdict) if claim.prior_verdict else None,
            ),
            task="plan_experiment",
            context={
                "subject": claim.subject,
                "default_repetitions": self._default_repetitions,
            },
            temperature=0.0,
        )

        _, outcome = self.reason(
            request,
            investigation_id=claim.investigation_id,
            validate=lambda payload: self._validate(payload, claim),
        )
        payload = outcome.payload
        plan = self._build_plan(payload, claim, moment, outcome)
        return plan, outcome

    def execute(self, plan: ExperimentPlan, claim: Claim) -> ExperimentResult:
        """Run the probe. Deterministic from here on.

        The tool-permission check is what makes "the Experimenter may call the target model"
        an enforced grant rather than an assumption.
        """
        self.require_write("evidence.raw")
        self.check_tool(
            "target_model.predict",
            risk=RiskLevel.LOW,
            investigation_id=claim.investigation_id,
            arguments={"experiment_id": plan.id, "repetitions": plan.repetitions},
        )
        return self._runner.run(plan, claim)

    # -- validation --------------------------------------------------------------------

    def _validate(self, payload: dict[str, Any], claim: Claim) -> dict[str, Any]:
        """Reject a plan that could not produce interpretable evidence.

        Runs before the plan object is built, so an unusable design costs one retry rather
        than a full experiment against the target model.
        """
        required = ("intervention_type", "target_variable", "control_variable")
        missing = [key for key in required if key not in payload]
        if missing:
            raise ValueError(f"probe plan is missing required keys: {missing}")

        intervention_type = str(payload["intervention_type"]).lower()
        if intervention_type not in VALID_INTERVENTIONS:
            raise ValueError(
                f"intervention_type must be one of {sorted(VALID_INTERVENTIONS)}, "
                f"got {intervention_type!r}"
            )
        payload["intervention_type"] = intervention_type

        target = sanitize_identifier(str(payload["target_variable"]))
        if target not in FEATURE_NAMES:
            raise ValueError(f"target_variable {target!r} is not a laboratory feature")
        if target != claim.subject:
            raise ValueError(
                f"the probe must intervene on the claim's subject {claim.subject!r}, "
                f"not {target!r}"
            )
        payload["target_variable"] = target

        control = payload.get("control_variable")
        if control is not None:
            control = sanitize_identifier(str(control))
            if control not in FEATURE_NAMES:
                raise ValueError(f"control_variable {control!r} is not a laboratory feature")
            if control == target:
                raise ValueError(
                    "the control must perturb a different variable than the intervention"
                )
            if control in claim.target_variables:
                raise ValueError(
                    f"control_variable {control!r} is one of the claim's own targets"
                )
        payload["control_variable"] = control

        if claim.primacy_claim and control is None:
            # Without a control there is no way to refute "this is the *primary* driver".
            raise ValueError(
                "a primacy claim requires a control variable, otherwise the claim's central "
                "assertion cannot be tested"
            )

        repetitions = int(payload.get("repetitions", self._default_repetitions))
        if not 3 <= repetitions <= 100:
            raise ValueError(f"repetitions must lie in [3, 100], got {repetitions}")
        payload["repetitions"] = repetitions

        threshold = float(payload.get("min_effect_threshold", 0.10))
        if not 0.0 < threshold <= 1.0:
            raise ValueError(f"min_effect_threshold must lie in (0, 1], got {threshold}")
        payload["min_effect_threshold"] = threshold

        return payload

    # -- construction ------------------------------------------------------------------

    def _build_plan(
        self,
        payload: dict[str, Any],
        claim: Claim,
        moment: datetime,
        outcome: ReasoningOutcome,
    ) -> ExperimentPlan:
        target = str(payload["target_variable"])
        control_name = payload.get("control_variable")
        intervention_type = InterventionType(payload["intervention_type"])

        intervention = InterventionSpec(
            variable=target,
            intervention_type=intervention_type,
            value=self._resolve_value(payload.get("intervention_value"), target, intervention_type),
            delta=self._resolve_delta(payload.get("intervention_delta"), intervention_type),
        )
        control = (
            InterventionSpec(
                variable=control_name,
                intervention_type=InterventionType.NEUTRALIZE,
                value=neutral_value(control_name),
            )
            if control_name
            else None
        )

        # Preserved features are computed, not taken from the model. An agent must not be
        # able to quietly shrink the constraint set that makes its own result meaningful.
        preserved = [
            name
            for name in FEATURE_NAMES
            if name != target and (control is None or name != control.variable)
        ]

        repetitions = int(payload["repetitions"])
        fixtures = fixture_set_for(claim.scope.distribution_version)

        return ExperimentPlan(
            id=experiment_id(claim.id, PROTOCOL_VERSION, self._default_seed, repetitions),
            claim_id=claim.id,
            investigation_id=claim.investigation_id,
            scope=claim.scope,
            protocol_version=PROTOCOL_VERSION,
            intervention=intervention,
            control=control,
            constraints=ConstraintSpec(
                preserved_features=preserved,
                tolerance=1e-9,
                feature_bounds={
                    name: (FEATURE_INDEX[name].minimum, FEATURE_INDEX[name].maximum)
                    for name in FEATURE_NAMES
                },
            ),
            fixture_set=fixtures.name,
            repetitions=repetitions,
            seed=self._default_seed,
            expected_direction=ExpectedDirection(str(claim.expected_direction)),
            min_effect_threshold=float(payload["min_effect_threshold"]),
            confounders=[
                sanitize_identifier(str(c)) for c in payload.get("confounders", [])
            ][:16],
            stopping_conditions=[
                str(s)[:200] for s in payload.get("stopping_conditions", [])
            ][:16],
            invalid_conditions=[
                str(s)[:200] for s in payload.get("invalid_conditions", [])
            ][:16],
            created_at=moment,
            provenance=outcome.provenance,
        )

    @staticmethod
    def _resolve_value(
        proposed: Any, variable: str, intervention_type: InterventionType
    ) -> float | None:
        """Resolve the target value, overriding a model-proposed neutral value.

        NEUTRALIZE has a defined meaning in the laboratory, and the Experimenter does not get
        to redefine it - otherwise "neutralize urgency" could quietly become "set urgency to
        0.94" and the probe would prove nothing while looking rigorous.
        """
        if intervention_type is InterventionType.NEUTRALIZE:
            return neutral_value(variable)
        if intervention_type is InterventionType.SUBSTITUTE:
            spec = FEATURE_INDEX[variable]
            value = float(proposed) if proposed is not None else spec.neutral_value
            return min(spec.maximum, max(spec.minimum, value))
        return None

    @staticmethod
    def _resolve_delta(proposed: Any, intervention_type: InterventionType) -> float | None:
        if intervention_type not in (InterventionType.INCREASE, InterventionType.DECREASE):
            return None
        if proposed is None:
            raise InterventionRejected(
                f"{intervention_type} requires an explicit delta and none was proposed"
            )
        delta = float(proposed)
        if intervention_type is InterventionType.INCREASE and delta <= 0:
            delta = abs(delta) or 0.2
        if intervention_type is InterventionType.DECREASE and delta >= 0:
            delta = -abs(delta) or -0.2
        return delta
