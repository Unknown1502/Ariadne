"""Operator-supplied configuration: connections, feature semantics, explanation sources.

These are the three things an organisation must supply before Ariadne can verify anything
about their model, and until now they existed only as constants in the laboratory. That was
honest for a laboratory and useless for a product: a governance team cannot point Ariadne at
their own model by editing `distributions.py`.

**Why these live in the runtime store and not the evidence ledger.** The ledger is
append-only because evidence must never change. Configuration is the opposite: a connection's
credentials rotate, a neutral value gets revised when a domain expert disagrees with it, a
model endpoint moves. Putting mutable configuration in an immutable store would either
corrupt the append-only guarantee or produce a ledger that is mostly noise.

The link between them is `configuration_version`. Every artifact already carries a
`VersionScope` saying which model and distribution it is true of; a feature definition
carries a version too, so "this verdict was produced under feature semantics v3" is
answerable. Changing a neutral value is a scientific act, not an edit, and it gets a new
version rather than overwriting the old one.

**What is deliberately not modelled here.** No credential is stored. A connection holds a
*reference* to a secret (a Secret Manager resource name, or an environment variable name),
never the secret itself, so a leaked configuration document leaks nothing that grants access.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from backend.core.schemas import AriadneModel

# -- identifiers -----------------------------------------------------------------------

CONNECTION_PREFIX = "CON"
FEATURE_PREFIX = "FTR"
EXPLANATION_SOURCE_PREFIX = "SRC"
EXPLANATION_PREFIX = "EXPL"


class ConnectionKind(StrEnum):
    """What a connection connects to.

    Each kind has genuinely different semantics and a genuinely different health check, which
    is why this is an enum and not a free-text label - a "test connection" button that does
    not know what it is testing cannot report anything trustworthy.
    """

    MODEL_ENDPOINT = "MODEL_ENDPOINT"
    MODEL_REGISTRY = "MODEL_REGISTRY"
    DRIFT_MONITOR = "DRIFT_MONITOR"
    EXPLANATION_SOURCE = "EXPLANATION_SOURCE"
    EVIDENCE_STORE = "EVIDENCE_STORE"


class ConnectionStatus(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    """Declared but never tested. Deliberately the default: a connection is not live because
    someone typed a URL into a form."""

    OK = "OK"
    FAILED = "FAILED"
    DISABLED = "DISABLED"


class TransportKind(StrEnum):
    VERTEX_AI = "VERTEX_AI"
    HTTP = "HTTP"
    PUBSUB = "PUBSUB"
    IN_PROCESS = "IN_PROCESS"
    """The synthetic laboratory. Named explicitly so a console can never present it as
    though it were somebody's production endpoint."""


class NeutralStrategy(StrEnum):
    """How the neutral value for a feature is determined.

    This is the single most consequential thing an integrator supplies. "Neutralize X" only
    means something if there is a defensible answer to "neutral according to what?", and the
    strategies here are the ones the experiment engine can actually execute - inventing a
    strategy the runner cannot perform would produce a configuration that validates and then
    fails at experiment time.
    """

    EXPLICIT = "EXPLICIT"
    """A number the domain expert supplies and defends."""

    POPULATION_MEDIAN = "POPULATION_MEDIAN"
    """The median over the declared distribution. Defensible when the population is the
    reference the decision is made against."""

    MIDPOINT = "MIDPOINT"
    """Midpoint of the declared range. Weakest of the three, and only honest for features
    whose range is itself meaningful."""

    REFERENCE_CATEGORY = "REFERENCE_CATEGORY"
    """For categorical features: the category that means "no signal", such as `unknown`."""


class FeatureDataType(StrEnum):
    CONTINUOUS = "CONTINUOUS"
    ORDINAL = "ORDINAL"
    CATEGORICAL = "CATEGORICAL"


class ExplanationSourceType(StrEnum):
    MODEL_RESPONSE = "MODEL_RESPONSE"
    """The prediction call returns the explanation alongside the score."""

    EXPLANATION_ENDPOINT = "EXPLANATION_ENDPOINT"
    """A second endpoint explains a prediction the first one made."""

    EXTERNAL_EVENT = "EXTERNAL_EVENT"
    """A governance system pushes EXPLANATION_RECEIVED to Ariadne."""


# -- connections -----------------------------------------------------------------------


class Connection(AriadneModel):
    """A declared link to something outside Ariadne.

    Status is *earned*, never declared. A freshly created connection is NOT_CONFIGURED until
    a real check succeeds, which is why `status` cannot be set through the create or update
    API - only `record_probe` moves it.
    """

    id: str
    kind: ConnectionKind
    name: str = Field(min_length=1, max_length=120)
    transport: TransportKind

    endpoint: str = ""
    """URL, Pub/Sub topic, or empty for IN_PROCESS."""

    model_id: str = ""
    model_version: str = ""
    distribution_version: str = ""
    project: str = ""
    region: str = ""

    credential_ref: str = ""
    """A *reference* to a secret - a Secret Manager resource name or an environment variable
    name - never the secret. Enforced by a validator, because the one thing worse than no
    secret management is secret management somebody bypassed once."""

    timeout_seconds: float = Field(default=10.0, gt=0.0, le=300.0)
    enabled: bool = True

    status: ConnectionStatus = ConnectionStatus.NOT_CONFIGURED
    last_checked_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None
    probe_detail: dict[str, Any] = Field(default_factory=dict)
    """What the last check actually observed, check by check, so a green tick can be
    audited rather than believed."""

    configuration_version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime

    @field_validator("credential_ref")
    @classmethod
    def _must_be_a_reference(cls, value: str) -> str:
        """Reject anything that looks like a secret rather than a pointer to one."""
        if not value:
            return value
        looks_like_secret = (
            value.startswith(("sk-", "AIza", "ya29.", "-----BEGIN"))
            or (len(value) > 80 and " " not in value and "/" not in value)
        )
        if looks_like_secret:
            raise ValueError(
                "credential_ref must reference a secret (a Secret Manager resource name or "
                "an environment variable name), not contain one"
            )
        return value

    @model_validator(mode="after")
    def _endpoint_required_unless_in_process(self) -> Connection:
        if self.transport is not TransportKind.IN_PROCESS and not self.endpoint:
            raise ValueError(f"{self.transport} connections need an endpoint")
        return self

    @property
    def is_live(self) -> bool:
        """The only definition of live in the system: enabled, and a real check passed."""
        return self.enabled and self.status is ConnectionStatus.OK


class ProbeCheck(AriadneModel):
    """One thing a connection test actually verified."""

    name: str
    passed: bool
    detail: str


class ProbeResult(AriadneModel):
    """The outcome of really talking to the other side."""

    connection_id: str
    ok: bool
    checks: list[ProbeCheck]
    error: str | None = None
    latency_ms: float = Field(ge=0.0)
    checked_at: datetime


# -- feature semantics -----------------------------------------------------------------


class FeatureSemantics(AriadneModel):
    """What it means to intervene on one feature of one model.

    Ariadne supplies the verification protocol. This is the half the model owner has to
    supply, and no amount of adapter code substitutes for it: neutralizing a feature requires
    knowing what neutral means for that feature in that domain, and getting it wrong produces
    arithmetic that works and verdicts that mean nothing.
    """

    id: str
    model_id: str
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    data_type: FeatureDataType

    minimum: float | None = None
    maximum: float | None = None
    allowed_values: list[str] = Field(default_factory=list)

    neutral_strategy: NeutralStrategy
    neutral_value: float | None = None
    neutral_category: str = ""

    codec: Literal["identity", "normalised", "one_hot"] = "identity"
    intervention_strategy: Literal["replace", "ablate"] = "replace"

    validated: bool = False
    """Set only by `validate_feature`, never by a caller. An unvalidated feature cannot be
    used in an experiment - the alternative is discovering the problem after spending money
    on model calls."""

    validation_errors: list[str] = Field(default_factory=list)
    configuration_version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _strategy_matches_type(self) -> FeatureSemantics:
        if self.data_type is FeatureDataType.CATEGORICAL:
            if self.neutral_strategy is not NeutralStrategy.REFERENCE_CATEGORY:
                raise ValueError(
                    "a categorical feature needs REFERENCE_CATEGORY; a median or midpoint "
                    "of unordered categories is not a value the model can be given"
                )
        elif self.neutral_strategy is NeutralStrategy.REFERENCE_CATEGORY:
            raise ValueError("REFERENCE_CATEGORY only applies to categorical features")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum >= self.maximum
        ):
            raise ValueError(f"range is inverted or empty: [{self.minimum}, {self.maximum}]")
        return self


def validate_feature(feature: FeatureSemantics) -> list[str]:
    """Every reason this feature cannot yet be intervened on, or an empty list.

    Deliberately returns *all* the problems rather than raising on the first. An integrator
    fixing a feature definition should see the whole list, not discover the next failure
    after each round trip.
    """
    problems: list[str] = []

    if feature.data_type is FeatureDataType.CATEGORICAL:  # noqa: SIM102 - branches differ
        if not feature.allowed_values:
            problems.append("a categorical feature must declare its allowed values")
        elif feature.neutral_category not in feature.allowed_values:
            problems.append(
                f"neutral category {feature.neutral_category!r} is not one of the declared "
                f"values {feature.allowed_values}"
            )
        return problems

    if feature.minimum is None or feature.maximum is None:
        problems.append("a numeric feature needs a declared range to intervene within")

    if feature.neutral_strategy is NeutralStrategy.EXPLICIT:
        if feature.neutral_value is None:
            problems.append(
                "EXPLICIT requires a neutral value. There is no default: a neutral value "
                "nobody chose is a counterfactual nobody can defend"
            )
        elif (
            feature.minimum is not None
            and feature.maximum is not None
            and not (feature.minimum <= feature.neutral_value <= feature.maximum)
        ):
            problems.append(
                f"neutral value {feature.neutral_value} lies outside the declared range "
                f"[{feature.minimum}, {feature.maximum}], so the intervention would "
                f"produce an input the model should never see"
            )
    elif feature.neutral_strategy is NeutralStrategy.MIDPOINT:
        if feature.minimum is None or feature.maximum is None:
            problems.append("MIDPOINT needs a declared range to take the midpoint of")
    elif (
        feature.neutral_strategy is NeutralStrategy.POPULATION_MEDIAN
        and feature.neutral_value is None
    ):
        problems.append(
            "POPULATION_MEDIAN requires the median to have been computed and supplied; "
            "Ariadne does not have your population"
        )

    return problems


def resolve_neutral(feature: FeatureSemantics) -> float | str:
    """The value an intervention will actually set. Raises if the feature is not valid."""
    problems = validate_feature(feature)
    if problems:
        raise ValueError(
            f"feature {feature.name!r} is not testable: " + "; ".join(problems)
        )
    if feature.data_type is FeatureDataType.CATEGORICAL:
        return feature.neutral_category
    if feature.neutral_strategy is NeutralStrategy.MIDPOINT:
        assert feature.minimum is not None and feature.maximum is not None
        return (feature.minimum + feature.maximum) / 2.0
    assert feature.neutral_value is not None
    return feature.neutral_value


# -- explanation sources ---------------------------------------------------------------


class ExplanationSource(AriadneModel):
    """Where a model's explanations enter Ariadne."""

    id: str
    model_id: str
    name: str = Field(min_length=1, max_length=120)
    source_type: ExplanationSourceType

    endpoint: str = ""
    explanation_field: str = "explanation"
    """Where in the payload the explanation text sits. Defaulted, because most responses
    call it this, and configurable because plenty do not."""

    decision_field: str = "decision"
    enabled: bool = True
    received_count: int = Field(default=0, ge=0)
    last_received_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _endpoint_required_for_endpoint_sources(self) -> ExplanationSource:
        if self.source_type is ExplanationSourceType.EXPLANATION_ENDPOINT and not self.endpoint:
            raise ValueError("EXPLANATION_ENDPOINT needs an endpoint to call")
        return self


class ReceivedExplanation(AriadneModel):
    """One explanation, as it arrived, before Ariadne interpreted anything.

    Stored verbatim on purpose. The claim compiled from it is an interpretation, and an
    interpretation whose source has been discarded cannot be audited or re-compiled when the
    compiler improves.
    """

    id: str
    source_id: str
    model_id: str
    model_version: str
    distribution_version: str
    prediction_id: str = ""
    decision: str = ""
    explanation: str = Field(min_length=1)
    received_at: datetime
    investigation_id: str | None = None
