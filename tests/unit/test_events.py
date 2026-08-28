"""Event contract tests.

The bus is where typing usually rots into `dict[str, Any]`. These tests hold the line, and
they cover the serialization path specifically because a payload that silently empties on
the wire is invisible until production.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.core.enums import EventType
from backend.events.schemas import (
    PAYLOAD_TYPES,
    AriadneEvent,
    DistributionChangedPayload,
    ExplanationReceivedPayload,
    ModelVersionDeployedPayload,
    make_event,
    parse_event,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def deployed_payload() -> ModelVersionDeployedPayload:
    return ModelVersionDeployedPayload(
        model_id="synthetic-triage",
        model_version="2.0.0",
        distribution_version="baseline_2024.1",
        deployed_at=T0,
    )


@pytest.fixture
def deployed_event(deployed_payload: ModelVersionDeployedPayload) -> AriadneEvent:
    return make_event(
        EventType.MODEL_VERSION_DEPLOYED,
        deployed_payload,
        aggregate_id="synthetic-triage",
        aggregate_version="2.0.0",
        occurred_at=T0,
    )


class TestPayloadRegistry:
    def test_every_event_type_declares_a_payload_contract(self) -> None:
        missing = {t for t in EventType if t not in PAYLOAD_TYPES}
        assert not missing, f"event types with no payload contract: {sorted(missing)}"

    def test_registry_has_no_entries_for_unknown_types(self) -> None:
        assert set(PAYLOAD_TYPES) <= set(EventType)


class TestEnvelope:
    def test_payload_must_match_the_declared_event_type(
        self, deployed_payload: ModelVersionDeployedPayload
    ) -> None:
        with pytest.raises(ValidationError, match="requires a DistributionChangedPayload"):
            AriadneEvent(
                event_id="EVT-1",
                event_type=EventType.DISTRIBUTION_CHANGED,
                aggregate_id="synthetic-triage",
                aggregate_version="2.0.0",
                occurred_at=T0,
                idempotency_key="0123456789ab",
                payload=deployed_payload,
            )

    def test_correct_payload_is_accepted(self, deployed_event: AriadneEvent) -> None:
        assert isinstance(deployed_event.payload, ModelVersionDeployedPayload)


class TestSerialization:
    def test_payload_survives_the_wire(self, deployed_event: AriadneEvent) -> None:
        # Regression guard: a base-class annotation silently serialized this to `{}`.
        dumped = deployed_event.model_dump(mode="json")
        assert dumped["payload"]["model_version"] == "2.0.0"
        assert dumped["payload"]["model_id"] == "synthetic-triage"

    def test_round_trip_restores_the_concrete_type(self, deployed_event: AriadneEvent) -> None:
        restored = parse_event(deployed_event.model_dump(mode="json"))
        assert isinstance(restored.payload, ModelVersionDeployedPayload)
        assert restored.payload == deployed_event.payload
        assert restored.idempotency_key == deployed_event.idempotency_key

    def test_parsing_an_unknown_event_type_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="no recognizable event_type"):
            parse_event({"event_type": "TAKE_OVER_THE_FLEET"})

    def test_parsing_a_malformed_payload_fails(self) -> None:
        raw = {
            "event_id": "EVT-1",
            "event_type": "MODEL_VERSION_DEPLOYED",
            "aggregate_id": "m",
            "aggregate_version": "2.0.0",
            "occurred_at": T0.isoformat(),
            "idempotency_key": "0123456789ab",
            "payload": {"model_id": "m", "model_version": "not-semver"},
        }
        with pytest.raises(ValidationError):
            parse_event(raw)


class TestIdempotency:
    def test_key_is_derived_from_the_work_not_the_envelope(
        self, deployed_payload: ModelVersionDeployedPayload
    ) -> None:
        # Two producers, two event_ids, one piece of work.
        first = make_event(
            EventType.MODEL_VERSION_DEPLOYED, deployed_payload,
            aggregate_id="synthetic-triage", aggregate_version="2.0.0",
            occurred_at=T0, producer="model-registry",
        )
        second = make_event(
            EventType.MODEL_VERSION_DEPLOYED, deployed_payload,
            aggregate_id="synthetic-triage", aggregate_version="2.0.0",
            occurred_at=T0, producer="ci-pipeline",
        )
        assert first.event_id != second.event_id
        assert first.idempotency_key == second.idempotency_key

    def test_different_versions_are_different_work(
        self, deployed_payload: ModelVersionDeployedPayload
    ) -> None:
        v2 = make_event(
            EventType.MODEL_VERSION_DEPLOYED, deployed_payload,
            aggregate_id="synthetic-triage", aggregate_version="2.0.0", occurred_at=T0,
        )
        v3 = make_event(
            EventType.MODEL_VERSION_DEPLOYED,
            deployed_payload.model_copy(update={"model_version": "3.0.0"}),
            aggregate_id="synthetic-triage", aggregate_version="3.0.0", occurred_at=T0,
        )
        assert v2.idempotency_key != v3.idempotency_key


class TestRetryEnvelope:
    def test_a_retry_is_the_same_work(self, deployed_event: AriadneEvent) -> None:
        retried = deployed_event.next_attempt()
        assert retried.attempt == 2
        assert retried.event_id == deployed_event.event_id
        assert retried.idempotency_key == deployed_event.idempotency_key

    def test_attempts_are_bounded(self, deployed_event: AriadneEvent) -> None:
        event = deployed_event
        for _ in range(AriadneEvent.MAX_ATTEMPTS - 1):
            assert not event.is_exhausted()
            event = event.next_attempt()
        assert event.is_exhausted()


class TestUntrustedText:
    def test_explanation_length_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            ExplanationReceivedPayload(
                model_id="m", model_version="1.0.0", distribution_version="d1",
                decision="HIGH", explanation="x" * 5000, received_at=T0,
            )

    def test_drift_score_is_a_unit_interval(self) -> None:
        with pytest.raises(ValidationError):
            DistributionChangedPayload(
                model_id="m", distribution_version="d2", drift_score=1.5, detected_at=T0
            )
