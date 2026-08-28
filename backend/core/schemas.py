"""Ariadne domain contracts.

Every object that crosses an agent boundary, enters the evidence ledger, or is persisted
is defined here as a Pydantic v2 model. The rules the whole system leans on:

  - ``extra="forbid"``: an agent cannot smuggle an unmodelled field past validation.
  - ``frozen=True``: domain objects are immutable. Append-only lineage is not a convention
    you have to remember to follow; mutation raises.
  - Every scoped object carries a VersionScope, so no conclusion can be read without the
    model version and data distribution it was true for.
  - Ranges are enforced at the boundary, so an out-of-range confidence never reaches the
    verifier.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.core.enums import (
    ExpectedDirection,
    GovernorAction,
    InterventionType,
    InvestigationState,
    LineageRelation,
    RunKind,
    VerdictStatus,
)
from backend.core.versions import PROTOCOL_VERSION, SCHEMA_VERSION, VERIFIER_VERSION

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
DISTRIBUTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

Unit = Annotated[float, Field(ge=0.0, le=1.0)]
"""A score constrained to [0, 1]. Used for every rate, ratio, and confidence."""


class AriadneModel(BaseModel):
    """Base contract: immutable, closed, and schema-versioned."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_assignment=True,
        str_strip_whitespace=True,
        protected_namespaces=(),  # `model_id` / `model_version` are domain terms here
        populate_by_name=True,
    )

    schema_version: str = SCHEMA_VERSION

    @field_validator("schema_version")
    @classmethod
    def _known_schema_version(cls, v: str) -> str:
        major = v.split(".")[0]
        if major != SCHEMA_VERSION.split(".")[0]:
            raise ValueError(
                f"incompatible schema_version {v!r}; this build reads {SCHEMA_VERSION!r}"
            )
        return v


class VersionScope(AriadneModel):
    """What a conclusion is true *of*.

    Ariadne never states a result without one of these attached. A verdict without a scope
    is an overclaim, and this type is how that overclaim is made structurally impossible.
    """

    model_id: str = Field(min_length=1, max_length=128)
    model_version: str
    distribution_version: str

    @field_validator("model_version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not SEMVER_RE.match(v):
            raise ValueError(f"model_version must be MAJOR.MINOR.PATCH, got {v!r}")
        return v

    @field_validator("distribution_version")
    @classmethod
    def _distribution(cls, v: str) -> str:
        if not DISTRIBUTION_RE.match(v):
            raise ValueError(f"distribution_version has an unsupported format: {v!r}")
        return v

    def matches(self, other: VersionScope) -> bool:
        return (
            self.model_id == other.model_id
            and self.model_version == other.model_version
            and self.distribution_version == other.distribution_version
        )

    def label(self) -> str:
        return f"{self.model_id}@{self.model_version}/{self.distribution_version}"


class AgentProvenance(AriadneModel):
    """Who produced a semantic artifact, and with what configuration.

    Recorded for every LLM-derived object so a later reviewer can tell whether a result
    changed because the model changed or because the prompt did.
    """

    agent_id: str = Field(min_length=1)
    agent_version: str
    role: str
    llm_model: str | None = None
    prompt_version: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    attempts: int = Field(default=1, ge=1, le=10)
    output_hash: str | None = None
    produced_at: datetime | None = None

    @field_validator("agent_version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not SEMVER_RE.match(v):
            raise ValueError(f"agent_version must be MAJOR.MINOR.PATCH, got {v!r}")
        return v


# --------------------------------------------------------------------------------------
# Claim
# --------------------------------------------------------------------------------------


class Claim(AriadneModel):
    """A natural-language explanation compiled into a testable behavioral hypothesis.

    The claim is deliberately *behavioral*: it predicts what the model will do under a
    declared intervention. It says nothing about the model's internals, and the schema
    gives it no field in which to try.
    """

    id: str
    claim_family_id: str
    investigation_id: str
    scope: VersionScope

    source_explanation: str = Field(min_length=1, max_length=4000)
    source_explanation_hash: str
    source_decision: str = Field(min_length=1, max_length=128)

    subject: str = Field(min_length=1, max_length=128)
    predicate: str = Field(min_length=1, max_length=128)
    object_: str = Field(min_length=1, max_length=128, alias="object")

    expected_direction: ExpectedDirection
    expected_effect: float | None = Field(default=None, ge=0.0, le=1.0)
    primacy_claim: bool = False
    """True when the explanation asserts the subject is the *primary* driver.

    This matters for verification: a primacy claim can be contradicted by a control
    variable producing a larger effect, even when the subject's own effect is real.
    """

    target_variables: list[str] = Field(min_length=1, max_length=16)
    preserved_constraints: list[str] = Field(default_factory=list, max_length=32)
    assumptions: list[str] = Field(default_factory=list, max_length=16)
    ambiguities: list[str] = Field(default_factory=list, max_length=16)

    testability_score: Unit
    confidence: Unit
    audit_priority: Unit = 0.5
    prior_verdict: VerdictStatus | None = None

    valid_from: datetime
    valid_until: datetime | None = None

    provenance: AgentProvenance
    quarantined: bool = False
    quarantine_reasons: list[str] = Field(default_factory=list)

    @field_validator("target_variables", "preserved_constraints")
    @classmethod
    def _no_blank_names(cls, v: list[str]) -> list[str]:
        cleaned = [item.strip() for item in v]
        if any(not item for item in cleaned):
            raise ValueError("variable names must not be blank")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("variable names must be unique")
        return cleaned

    @model_validator(mode="after")
    def _coherent(self) -> Claim:
        overlap = set(self.target_variables) & set(self.preserved_constraints)
        if overlap:
            raise ValueError(
                f"variables cannot be both intervened on and preserved: {sorted(overlap)}"
            )
        if self.subject not in self.target_variables:
            raise ValueError(f"claim subject {self.subject!r} must appear in target_variables")
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        if self.quarantined and not self.quarantine_reasons:
            raise ValueError("a quarantined claim must record why")
        return self

    def is_valid_at(self, moment: datetime) -> bool:
        if moment < self.valid_from:
            return False
        return self.valid_until is None or moment < self.valid_until


# --------------------------------------------------------------------------------------
# Experiment plan
# --------------------------------------------------------------------------------------


class InterventionSpec(AriadneModel):
    """Exactly one variable, changed in exactly one declared way."""

    variable: str = Field(min_length=1, max_length=128)
    intervention_type: InterventionType
    value: float | None = Field(default=None, ge=-1e6, le=1e6)
    delta: float | None = Field(default=None, ge=-1e6, le=1e6)

    @model_validator(mode="after")
    def _parameters_match_type(self) -> InterventionSpec:
        needs_value = {InterventionType.NEUTRALIZE, InterventionType.SUBSTITUTE}
        needs_delta = {InterventionType.INCREASE, InterventionType.DECREASE}
        if self.intervention_type in needs_value and self.value is None:
            raise ValueError(f"{self.intervention_type} requires an explicit target value")
        if self.intervention_type in needs_delta and self.delta is None:
            raise ValueError(f"{self.intervention_type} requires an explicit delta")
        if self.intervention_type is InterventionType.INCREASE and (self.delta or 0.0) <= 0:
            raise ValueError("an 'increase' intervention needs a positive delta")
        if self.intervention_type is InterventionType.DECREASE and (self.delta or 0.0) >= 0:
            raise ValueError("a 'decrease' intervention needs a negative delta")
        return self


class ConstraintSpec(AriadneModel):
    """What must stay the same for the intervention to mean anything.

    If an intervention silently perturbs other features, the observed effect is not
    attributable to the claimed variable. The engine checks this before trusting a run.
    """

    preserved_features: list[str] = Field(default_factory=list, max_length=32)
    tolerance: float = Field(default=1e-9, ge=0.0, le=1.0)
    feature_bounds: dict[str, tuple[float, float]] = Field(default_factory=dict)
    require_realistic_range: bool = True

    @field_validator("feature_bounds")
    @classmethod
    def _ordered_bounds(cls, v: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
        for name, (low, high) in v.items():
            if low > high:
                raise ValueError(f"bounds for {name!r} are inverted: {low} > {high}")
        return v


class ExperimentPlan(AriadneModel):
    """The full, executable description of one probe.

    Everything that could change the outcome is on this object, including the seed. That is
    what makes an experiment reproducible from its record alone.
    """

    id: str
    claim_id: str
    investigation_id: str
    scope: VersionScope
    protocol_version: str = PROTOCOL_VERSION

    intervention: InterventionSpec
    control: InterventionSpec | None = None
    constraints: ConstraintSpec

    fixture_set: str = Field(min_length=1)
    repetitions: int = Field(ge=1, le=100)
    seed: int = Field(ge=0, le=2**31 - 1)

    expected_direction: ExpectedDirection
    min_effect_threshold: float = Field(default=0.10, ge=0.0, le=1.0)
    reproducibility_threshold: Unit = 0.80
    validity_threshold: Unit = 0.90
    min_repetitions_for_verdict: int = Field(default=3, ge=1, le=100)
    instability_threshold: float = Field(default=0.02, ge=0.0, le=1.0)

    stopping_conditions: list[str] = Field(default_factory=list, max_length=16)
    confounders: list[str] = Field(default_factory=list, max_length=16)
    invalid_conditions: list[str] = Field(default_factory=list, max_length=16)

    created_at: datetime
    """When the plan was compiled. Required because a plan is a persisted record in its own
    right, and an evidence ledger ordered by time cannot hold a row with no time."""

    provenance: AgentProvenance

    @model_validator(mode="after")
    def _coherent(self) -> ExperimentPlan:
        if self.control is not None and self.control.variable == self.intervention.variable:
            raise ValueError(
                "the control must perturb a different variable than the intervention, "
                "otherwise it is not a control"
            )
        if self.intervention.variable in self.constraints.preserved_features:
            raise ValueError(
                f"{self.intervention.variable!r} cannot be both intervened on and preserved"
            )
        if self.repetitions < self.min_repetitions_for_verdict:
            raise ValueError(
                f"repetitions ({self.repetitions}) is below the plan's own minimum for a "
                f"verdict ({self.min_repetitions_for_verdict})"
            )
        return self


# --------------------------------------------------------------------------------------
# Execution and evidence
# --------------------------------------------------------------------------------------


class ExperimentRun(AriadneModel):
    """One target-model call, with its inputs and outputs hashed."""

    id: str
    experiment_id: str
    kind: RunKind
    index: int = Field(ge=0, le=1000)
    scope: VersionScope

    features: dict[str, float]
    score: float
    decision: str
    model_explanation: str

    input_hash: str
    output_hash: str
    executed_at: datetime
    duration_ms: float = Field(ge=0.0)

    @field_validator("features")
    @classmethod
    def _non_empty_finite(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("a run must record the feature vector it was given")
        for name, value in v.items():
            if not math.isfinite(value):
                raise ValueError(f"feature {name!r} is not finite")
        return v


class RunSummary(AriadneModel):
    """Aggregate statistics for one arm of an experiment."""

    kind: RunKind
    n: int = Field(ge=0, le=1000)
    mean: float
    stdev: float = Field(ge=0.0)
    minimum: float
    maximum: float
    scores: list[float] = Field(default_factory=list, max_length=1000)
    run_ids: list[str] = Field(default_factory=list, max_length=1000)

    SUMMARY_TOLERANCE: ClassVar[float] = 1e-9
    """Floating-point summation can place a legitimate mean a few ULPs outside
    [minimum, maximum] (mean([0.7, 0.7, 0.7]) == 0.7000000000000001). The check is a
    sanity guard against transposed or fabricated summaries, not a numerics test, so it
    tolerates error far below any effect size Ariadne can measure."""

    @model_validator(mode="after")
    def _consistent(self) -> RunSummary:
        if self.n != len(self.scores):
            raise ValueError(f"n={self.n} does not match {len(self.scores)} recorded scores")
        if self.n:
            tol = self.SUMMARY_TOLERANCE
            if not (self.minimum - tol <= self.mean <= self.maximum + tol):
                raise ValueError(
                    f"mean {self.mean} lies outside the observed range "
                    f"[{self.minimum}, {self.maximum}]"
                )
            if self.minimum > self.maximum:
                raise ValueError("minimum exceeds maximum")
        return self


class Evidence(AriadneModel):
    """The immutable, hashed record of what an experiment actually produced.

    Evidence holds measurements only. It contains no verdict field, because the object that
    records observations must not be the object that draws conclusions.
    """

    id: str
    experiment_id: str
    claim_id: str
    claim_family_id: str
    scope: VersionScope
    protocol_version: str

    baseline: RunSummary
    intervention: RunSummary
    control: RunSummary | None = None

    effect_size: float
    effect_ci: tuple[float, float] | None = None
    control_effect_size: float | None = None
    reproducibility: Unit
    validity_score: Unit
    instability: float = Field(ge=0.0)

    run_ids: list[str] = Field(min_length=1)
    input_hashes: list[str] = Field(min_length=1)
    output_hashes: list[str] = Field(min_length=1)
    evidence_hash: str
    raw_artifact_uri: str | None = None
    created_at: datetime

    @model_validator(mode="after")
    def _arms_align(self) -> Evidence:
        if self.baseline.kind is not RunKind.BASELINE:
            raise ValueError("baseline summary must be of kind BASELINE")
        if self.intervention.kind is not RunKind.INTERVENTION:
            raise ValueError("intervention summary must be of kind INTERVENTION")
        if self.control is not None and self.control.kind is not RunKind.CONTROL:
            raise ValueError("control summary must be of kind CONTROL")
        if self.baseline.n != self.intervention.n:
            raise ValueError(
                f"paired comparison requires equal arms: baseline n={self.baseline.n}, "
                f"intervention n={self.intervention.n}"
            )
        if self.effect_ci is not None and self.effect_ci[0] > self.effect_ci[1]:
            raise ValueError("confidence interval bounds are inverted")
        return self


# --------------------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------------------


class Verdict(AriadneModel):
    """The deterministic conclusion. Produced only by the Verifier, never by an LLM."""

    id: str
    claim_id: str
    claim_family_id: str
    scope: VersionScope
    protocol_version: str

    status: VerdictStatus
    behavioral_support: Unit
    intervention_validity: Unit
    reproducibility: Unit
    contradiction_score: Unit

    effect_size: float
    control_effect_size: float | None = None
    expected_direction: ExpectedDirection
    observed_direction: ExpectedDirection

    evidence_ids: list[str] = Field(min_length=1)
    reason_codes: list[str] = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=2000)
    verifier_version: str = VERIFIER_VERSION
    created_at: datetime

    @field_validator("verifier_version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not SEMVER_RE.match(v):
            raise ValueError(f"verifier_version must be MAJOR.MINOR.PATCH, got {v!r}")
        return v


# --------------------------------------------------------------------------------------
# Lineage
# --------------------------------------------------------------------------------------


class LineageEntry(AriadneModel):
    """One append-only row in a claim family's history.

    A new result never edits an old one. It appends an entry whose ``relation`` states what
    it does to the previous entry, and ``previous_entry_hash`` chains it so a deleted or
    altered ancestor is detectable.
    """

    id: str
    claim_family_id: str
    claim_id: str
    scope: VersionScope
    protocol_version: str

    verdict_id: str
    status: VerdictStatus
    evidence_ids: list[str] = Field(min_length=1)

    behavioral_support: Unit
    intervention_validity: Unit
    reproducibility: Unit
    effect_size: float

    relation: LineageRelation
    supersedes_entry_id: str | None = None

    valid_from: datetime
    valid_until: datetime | None = None
    expired_reason: str | None = None
    created_at: datetime

    input_hashes: list[str] = Field(min_length=1)
    output_hashes: list[str] = Field(min_length=1)
    verifier_version: str
    previous_entry_hash: str | None = None
    entry_hash: str

    @model_validator(mode="after")
    def _relation_is_consistent(self) -> LineageEntry:
        if self.relation is LineageRelation.INITIAL and self.supersedes_entry_id is not None:
            raise ValueError("an INITIAL entry cannot supersede anything")
        if (
            self.relation in (LineageRelation.SUPERSEDES, LineageRelation.DISPUTES)
            and self.supersedes_entry_id is None
        ):
            raise ValueError(f"a {self.relation} entry must name the entry it acts on")
        if self.valid_until is not None and self.valid_until < self.valid_from:
            raise ValueError("valid_until must not precede valid_from")
        if self.valid_until is not None and not self.expired_reason:
            raise ValueError("closing a validity window requires a recorded reason")
        return self

    def is_current_at(self, moment: datetime) -> bool:
        if moment < self.valid_from:
            return False
        return self.valid_until is None or moment < self.valid_until


# --------------------------------------------------------------------------------------
# Explanation Debt
# --------------------------------------------------------------------------------------


class DebtComponent(AriadneModel):
    """One weighted contributor to Explanation Debt, with its arithmetic exposed.

    ``ratio * weight == points`` always holds, so a reader can check the number rather than
    trust it.
    """

    name: str = Field(min_length=1)
    ratio: Unit
    weight: float = Field(ge=0.0, le=100.0)
    points: float = Field(ge=0.0, le=100.0)
    detail: str = Field(default="", max_length=500)
    contributing_ids: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _arithmetic_holds(self) -> DebtComponent:
        if abs(self.ratio * self.weight - self.points) > 1e-6:
            raise ValueError(
                f"component {self.name!r} is not self-consistent: "
                f"{self.ratio} * {self.weight} != {self.points}"
            )
        return self


class DebtSnapshot(AriadneModel):
    """An immutable point-in-time debt reading. Snapshots accumulate; none is ever edited."""

    id: str
    model_id: str
    scope_label: str
    policy_version: str
    components: list[DebtComponent] = Field(min_length=1)
    total: float = Field(ge=0.0, le=100.0)
    previous_total: float | None = Field(default=None, ge=0.0, le=100.0)
    computed_at: datetime
    trigger_event_id: str | None = None

    @model_validator(mode="after")
    def _total_matches_components(self) -> DebtSnapshot:
        expected = sum(c.points for c in self.components)
        if abs(expected - self.total) > 1e-6:
            raise ValueError(
                f"debt total {self.total} does not equal the sum of its components {expected}"
            )
        return self

    @property
    def delta(self) -> float | None:
        if self.previous_total is None:
            return None
        return round(self.total - self.previous_total, 6)


# --------------------------------------------------------------------------------------
# Governance
# --------------------------------------------------------------------------------------


class GovernorDecision(AriadneModel):
    """A policy action, chosen by deterministic code.

    An LLM may *recommend* an action; ``recommendation`` records what it suggested and
    ``recommendation_accepted`` records whether policy agreed. That separation is what keeps
    the recommendation auditable instead of authoritative.
    """

    id: str
    investigation_id: str
    claim_family_id: str
    scope: VersionScope

    action: GovernorAction
    reason_codes: list[str] = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=2000)
    policy_version: str
    debt_snapshot_id: str | None = None
    debt_total: float | None = Field(default=None, ge=0.0, le=100.0)

    recommendation: GovernorAction | None = None
    recommendation_accepted: bool = True
    recommendation_provenance: AgentProvenance | None = None

    required_approval: bool = False
    next_event_at: datetime | None = None
    created_at: datetime

    @model_validator(mode="after")
    def _recommendation_is_tracked(self) -> GovernorDecision:
        if self.recommendation is not None:
            accepted = self.recommendation == self.action
            if accepted != self.recommendation_accepted:
                raise ValueError(
                    "recommendation_accepted must reflect whether the enforced action "
                    "equals the recommended action"
                )
        return self


class ApprovalRequest(AriadneModel):
    """A high-impact action waiting on a human.

    The action is not executed while this is PENDING; the request is the gate, not a
    notification about something already done.
    """

    id: str
    decision_id: str
    investigation_id: str
    action: GovernorAction
    justification: str = Field(min_length=1, max_length=2000)
    status: str = Field(default="PENDING", pattern=r"^(PENDING|APPROVED|REJECTED|EXPIRED)$")
    requested_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _decision_is_complete(self) -> ApprovalRequest:
        if self.status in ("APPROVED", "REJECTED") and (
            self.decided_at is None or not self.decided_by
        ):
            raise ValueError(f"a {self.status} request must record who decided and when")
        return self


# --------------------------------------------------------------------------------------
# Investigation
# --------------------------------------------------------------------------------------


class Investigation(AriadneModel):
    """The unit of work an event creates and a worker drives to completion."""

    id: str
    scope: VersionScope
    state: InvestigationState
    trigger_event_id: str
    trigger_event_type: str
    priority: Unit = 0.5

    claim_family_id: str | None = None
    claim_id: str | None = None
    experiment_id: str | None = None
    evidence_id: str | None = None
    verdict_id: str | None = None
    lineage_entry_id: str | None = None
    debt_snapshot_id: str | None = None
    decision_id: str | None = None

    source_decision: str | None = None
    source_explanation: str | None = Field(default=None, max_length=4000)

    attempts: int = Field(default=0, ge=0, le=100)
    last_error: str | None = Field(default=None, max_length=1000)
    created_at: datetime
    updated_at: datetime

    def with_state(self, state: InvestigationState, now: datetime, **fields: Any) -> Investigation:
        """Return a new Investigation in the given state.

        Investigations are frozen, so progress is expressed by replacing the record rather
        than mutating it. Callers should route this through the state machine, which is
        what enforces that the transition is legal.
        """
        return self.model_copy(update={"state": state, "updated_at": now, **fields})
