"""Typed events.

Ariadne's runtime is event-driven, and an event bus is exactly where untyped dictionaries
become permanent. Each event type therefore has a declared payload model, and the envelope
refuses to hold a payload that does not match its own ``event_type``.

Two envelope fields carry the reliability guarantees:

  - ``event_id`` identifies *this delivery*. Redeliveries reuse it.
  - ``idempotency_key`` identifies *the work*. Two different events that mean the same work
    share a key, and the worker executes it once.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import Field, SerializeAsAny, field_validator, model_validator

from backend.core.enums import EventType, GovernorAction, VerdictStatus
from backend.core.ids import idempotency_key as derive_idempotency_key
from backend.core.ids import random_id
from backend.core.schemas import SEMVER_RE, AriadneModel, Unit, VersionScope


class EventPayload(AriadneModel):
    """Base class for every typed event payload."""


class ModelVersionDeployedPayload(EventPayload):
    """A new model version went live. This is the event that wakes Ariadne up.

    Nothing in this payload asks for an investigation. Ariadne decides, on its own, which
    prior claims this version puts back in question.
    """

    model_id: str = Field(min_length=1)
    model_version: str
    distribution_version: str
    deployed_at: datetime
    deployed_by: str | None = None
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("model_version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not SEMVER_RE.match(v):
            raise ValueError(f"model_version must be MAJOR.MINOR.PATCH, got {v!r}")
        return v


class DistributionChangedPayload(EventPayload):
    """Input data drifted. Evidence gathered under the old distribution stops being current.

    Note this expires evidence rather than reversing verdicts: the old result stays true of
    the old distribution, which is precisely what append-only lineage is for.
    """

    model_id: str = Field(min_length=1)
    distribution_version: str
    previous_distribution_version: str | None = None
    drift_score: Unit = 0.0
    affected_features: list[str] = Field(default_factory=list, max_length=32)
    detected_at: datetime


class ExplanationReceivedPayload(EventPayload):
    """A decision and its natural-language explanation arrived from the target system.

    ``explanation`` is untrusted external text. It is transported as data and is never
    concatenated into an instruction position without going through the sanitizer first.
    """

    model_id: str = Field(min_length=1)
    model_version: str
    distribution_version: str
    decision: str = Field(min_length=1, max_length=128)
    explanation: str = Field(min_length=1, max_length=4000)
    case_id: str | None = None
    received_at: datetime


class ClaimExtractionFailedPayload(EventPayload):
    """The Investigator could not produce a valid Claim within its retry budget."""

    investigation_id: str
    model_id: str
    reason_code: str
    detail: str = Field(max_length=1000)
    attempts: int = Field(ge=1, le=10)


class ExperimentRequestedPayload(EventPayload):
    investigation_id: str
    claim_id: str
    experiment_id: str
    scope: VersionScope


class ExperimentRejectedPayload(EventPayload):
    """A plan failed deterministic validation and was never executed."""

    investigation_id: str
    claim_id: str
    reason_code: str
    detail: str = Field(max_length=1000)


class ExperimentFailedPayload(EventPayload):
    investigation_id: str
    experiment_id: str
    reason_code: str
    detail: str = Field(max_length=1000)
    retryable: bool = True


class ExperimentCompletedPayload(EventPayload):
    investigation_id: str
    experiment_id: str
    evidence_id: str
    scope: VersionScope


class VerdictCreatedPayload(EventPayload):
    investigation_id: str
    claim_id: str
    claim_family_id: str
    verdict_id: str
    status: VerdictStatus
    scope: VersionScope


class LineageUpdatedPayload(EventPayload):
    claim_family_id: str
    entry_id: str
    status: VerdictStatus
    scope: VersionScope


class EvidenceExpiredPayload(EventPayload):
    claim_family_id: str
    entry_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)


class DebtUpdatedPayload(EventPayload):
    model_id: str
    snapshot_id: str
    total: float = Field(ge=0.0, le=100.0)
    previous_total: float | None = Field(default=None, ge=0.0, le=100.0)


class ReviewRequiredPayload(EventPayload):
    investigation_id: str
    decision_id: str
    action: GovernorAction
    justification: str = Field(max_length=2000)


class AuditScheduledPayload(EventPayload):
    claim_family_id: str
    model_id: str
    scheduled_for: datetime
    priority: Unit
    reason_code: str


class AgentQuarantinedPayload(EventPayload):
    agent_id: str
    investigation_id: str | None = None
    reason_code: str
    detail: str = Field(max_length=1000)


class ModelRegisteredPayload(EventPayload):
    model_id: str = Field(min_length=1)
    owner: str | None = None
    description: str | None = Field(default=None, max_length=1000)
    registered_at: datetime


PAYLOAD_TYPES: dict[EventType, type[EventPayload]] = {
    EventType.MODEL_REGISTERED: ModelRegisteredPayload,
    EventType.MODEL_VERSION_DEPLOYED: ModelVersionDeployedPayload,
    EventType.DISTRIBUTION_CHANGED: DistributionChangedPayload,
    EventType.EXPLANATION_RECEIVED: ExplanationReceivedPayload,
    EventType.CLAIM_EXTRACTION_FAILED: ClaimExtractionFailedPayload,
    EventType.EXPERIMENT_REQUESTED: ExperimentRequestedPayload,
    EventType.EXPERIMENT_REJECTED: ExperimentRejectedPayload,
    EventType.EXPERIMENT_FAILED: ExperimentFailedPayload,
    EventType.EXPERIMENT_COMPLETED: ExperimentCompletedPayload,
    EventType.VERDICT_CREATED: VerdictCreatedPayload,
    EventType.LINEAGE_UPDATED: LineageUpdatedPayload,
    EventType.EVIDENCE_EXPIRED: EvidenceExpiredPayload,
    EventType.DEBT_UPDATED: DebtUpdatedPayload,
    EventType.REVIEW_REQUIRED: ReviewRequiredPayload,
    EventType.AUDIT_SCHEDULED: AuditScheduledPayload,
    EventType.AGENT_QUARANTINED: AgentQuarantinedPayload,
}
"""Every event type has exactly one payload contract. A type added to the enum without an
entry here fails the contract test, so the two cannot drift apart."""


class AriadneEvent(AriadneModel):
    """The event envelope.

    ``aggregate_id`` plus ``aggregate_version`` say what the event is *about*, which is
    what the idempotency key is derived from. That is deliberate: two producers emitting
    "v2.0.0 of the triage model deployed" describe one piece of work, not two.
    """

    MAX_ATTEMPTS: ClassVar[int] = 5

    event_id: str = Field(min_length=1)
    event_type: EventType
    aggregate_id: str = Field(min_length=1)
    aggregate_version: str = Field(min_length=1)
    occurred_at: datetime
    idempotency_key: str = Field(min_length=8)
    payload: SerializeAsAny[EventPayload]
    """Serialized as its concrete subclass.

    Without SerializeAsAny, Pydantic serializes against the declared base type and
    silently emits an empty payload -- every event would cross the bus with its
    contents stripped, and nothing would warn about it.
    """

    producer: str = "ariadne"
    trace_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    attempt: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def _payload_matches_type(self) -> AriadneEvent:
        expected = PAYLOAD_TYPES.get(self.event_type)
        if expected is None:
            raise ValueError(f"no payload contract registered for {self.event_type}")
        if not isinstance(self.payload, expected):
            raise ValueError(
                f"{self.event_type} requires a {expected.__name__}, "
                f"got {type(self.payload).__name__}"
            )
        return self

    def next_attempt(self) -> AriadneEvent:
        """Return the same event marked as one more delivery attempt.

        The event_id and idempotency_key are preserved on purpose: a retry is the same
        work, and must not be able to produce a second set of side effects.
        """
        return self.model_copy(update={"attempt": self.attempt + 1})

    def is_exhausted(self) -> bool:
        return self.attempt >= self.MAX_ATTEMPTS


def make_event(
    event_type: EventType,
    payload: EventPayload,
    *,
    aggregate_id: str,
    aggregate_version: str,
    occurred_at: datetime,
    trace_id: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    producer: str = "ariadne",
    event_id: str | None = None,
) -> AriadneEvent:
    """Build a well-formed event with a derived idempotency key.

    Always prefer this over constructing AriadneEvent directly: it guarantees the
    idempotency key is a function of the work, which is the property the whole
    at-least-once story rests on.
    """
    return AriadneEvent(
        event_id=event_id or random_id("EVT"),
        event_type=event_type,
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        occurred_at=occurred_at,
        idempotency_key=derive_idempotency_key(
            str(event_type), aggregate_id, aggregate_version
        ),
        payload=payload,
        producer=producer,
        trace_id=trace_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )


def parse_event(raw: dict[str, Any]) -> AriadneEvent:
    """Rebuild an event from transport (Pub/Sub JSON, a file, a test fixture).

    The payload is parsed with the concrete type its ``event_type`` declares, so an event
    arriving off the wire gets the same validation as one built in-process.
    """
    try:
        event_type = EventType(raw["event_type"])
    except (KeyError, ValueError) as exc:
        raise ValueError(
            f"event has no recognizable event_type: {raw.get('event_type')!r}"
        ) from exc
    payload_cls = PAYLOAD_TYPES[event_type]
    data = dict(raw)
    payload = data.get("payload")
    if isinstance(payload, dict):
        data["payload"] = payload_cls.model_validate(payload)
    return AriadneEvent.model_validate(data)
