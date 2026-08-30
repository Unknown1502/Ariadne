"""Configuration API: connections, feature semantics, explanation sources.

The three things an organisation must supply before Ariadne can verify anything about their
model. Until now they were constants in the laboratory, which is honest for a laboratory and
useless for a product - a governance team could not point Ariadne at their own model without
editing source.

Three rules run through every route here, and each exists because the alternative produces a
console that lies:

**Status is earned, never asserted.** `POST /connections` creates a connection in
NOT_CONFIGURED, and nothing but a real probe moves it to OK. The create and update bodies
have no status field at all, so there is no path by which typing a URL into a form makes
something look connected.

**Validation is scientific, not cosmetic.** A feature is not usable because it parsed. It is
usable when a neutral value exists, is correctly typed, sits inside the declared range, and
resolves to something the engine can actually set. `validated` is set by the validator or not
at all.

**A configuration error is never a scientific verdict.** Nothing in this module can produce
SUPPORTED, CONTRADICTED, or INCONCLUSIVE. An unreachable endpoint yields a FAILED connection;
an undefined neutral value yields a NOT_TESTABLE feature. Infrastructure problems and
scientific results stay in different vocabularies on purpose.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend.core.configuration import (
    CONNECTION_PREFIX,
    EXPLANATION_PREFIX,
    EXPLANATION_SOURCE_PREFIX,
    FEATURE_PREFIX,
    Connection,
    ConnectionKind,
    ConnectionStatus,
    ExplanationSource,
    ExplanationSourceType,
    FeatureDataType,
    FeatureSemantics,
    NeutralStrategy,
    ReceivedExplanation,
    TransportKind,
    resolve_neutral,
    validate_feature,
)
from backend.core.ids import random_id
from backend.integrations.prober import ConnectionProber, apply_probe

router = APIRouter(prefix="/api/v1", tags=["configuration"])


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------------
# Request bodies. Note what is absent: none of them carries `status` or `validated`.
# --------------------------------------------------------------------------------------


class ConnectionBody(BaseModel):
    kind: ConnectionKind
    name: str = Field(min_length=1, max_length=120)
    transport: TransportKind
    endpoint: str = ""
    model_id: str = ""
    model_version: str = ""
    distribution_version: str = ""
    project: str = ""
    region: str = ""
    credential_ref: str = ""
    timeout_seconds: float = Field(default=10.0, gt=0.0, le=300.0)

    model_config = {"protected_namespaces": ()}


class ConnectionPatch(BaseModel):
    name: str | None = None
    endpoint: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    distribution_version: str | None = None
    project: str | None = None
    region: str | None = None
    credential_ref: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0.0, le=300.0)

    model_config = {"protected_namespaces": ()}


class FeatureBody(BaseModel):
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
    codec: str = "identity"
    intervention_strategy: str = "replace"

    model_config = {"protected_namespaces": ()}


class ExplanationSourceBody(BaseModel):
    model_id: str
    name: str = Field(min_length=1, max_length=120)
    source_type: ExplanationSourceType
    endpoint: str = ""
    explanation_field: str = "explanation"
    decision_field: str = "decision"

    model_config = {"protected_namespaces": ()}


class IngestBody(BaseModel):
    """One explanation arriving from a registered source.

    `decision` is required, not defaulted. An explanation explains a decision, and "why did
    you decide X" is not a testable question without X - defaulting it to an empty string
    would let a caller ingest an explanation of nothing and have it look well formed.
    """

    model_version: str
    distribution_version: str
    explanation: str = Field(min_length=1)
    decision: str = Field(min_length=1)
    prediction_id: str = ""

    model_config = {"protected_namespaces": ()}


# --------------------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------------------


def _state() -> Any:
    from backend.api.main import app_state

    return app_state()


@router.get("/connections")
def list_connections() -> dict[str, Any]:
    connections = _state().runtime.list_connections()
    return {
        "connections": [c.model_dump(mode="json") for c in connections],
        "live": sum(1 for c in connections if c.is_live),
        "total": len(connections),
    }


@router.post("/connections", status_code=201)
def create_connection(body: ConnectionBody) -> dict[str, Any]:
    """Create a connection. It is NOT_CONFIGURED until a probe says otherwise."""
    moment = _now()
    try:
        connection = Connection(
            id=random_id(CONNECTION_PREFIX),
            created_at=moment,
            updated_at=moment,
            **body.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _state().runtime.save_connection(connection)
    return connection.model_dump(mode="json")


@router.get("/connections/{connection_id}")
def get_connection(connection_id: str) -> dict[str, Any]:
    connection = _state().runtime.get_connection(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail=f"no connection {connection_id}")
    return connection.model_dump(mode="json")


@router.patch("/connections/{connection_id}")
def update_connection(connection_id: str, body: ConnectionPatch) -> dict[str, Any]:
    """Editing configuration invalidates the previous probe result.

    Changing an endpoint and keeping the green tick from the *old* endpoint is exactly the
    kind of stale-truth this whole project argues against, so a configuration change resets
    status to NOT_CONFIGURED and bumps `configuration_version`.
    """
    runtime = _state().runtime
    connection = runtime.get_connection(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail=f"no connection {connection_id}")

    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        return connection.model_dump(mode="json")
    try:
        updated = connection.model_copy(
            update={
                **changes,
                "status": ConnectionStatus.NOT_CONFIGURED,
                "last_error": None,
                "probe_detail": {},
                "configuration_version": connection.configuration_version + 1,
                "updated_at": _now(),
            }
        )
        Connection.model_validate(updated.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    runtime.save_connection(updated)
    return updated.model_dump(mode="json")


@router.delete("/connections/{connection_id}", status_code=204)
def delete_connection(connection_id: str) -> None:
    if not _state().runtime.delete_connection(connection_id):
        raise HTTPException(status_code=404, detail=f"no connection {connection_id}")


@router.post("/connections/{connection_id}/test")
def test_connection(connection_id: str) -> dict[str, Any]:
    """Really talk to the other side, and record what was observed."""
    runtime = _state().runtime
    connection = runtime.get_connection(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail=f"no connection {connection_id}")

    result = ConnectionProber().probe(connection)
    runtime.save_connection(apply_probe(connection, result))
    return result.model_dump(mode="json")


@router.post("/connections/{connection_id}/enable")
def enable_connection(connection_id: str) -> dict[str, Any]:
    return _set_enabled(connection_id, True)


@router.post("/connections/{connection_id}/disable")
def disable_connection(connection_id: str) -> dict[str, Any]:
    return _set_enabled(connection_id, False)


def _set_enabled(connection_id: str, enabled: bool) -> dict[str, Any]:
    runtime = _state().runtime
    connection = runtime.get_connection(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail=f"no connection {connection_id}")
    updated = connection.model_copy(
        update={
            "enabled": enabled,
            "status": connection.status if enabled else ConnectionStatus.DISABLED,
            "updated_at": _now(),
        }
    )
    runtime.save_connection(updated)
    return updated.model_dump(mode="json")


# --------------------------------------------------------------------------------------
# Feature semantics
# --------------------------------------------------------------------------------------


@router.get("/feature-semantics")
def list_features(model_id: str | None = Query(default=None)) -> dict[str, Any]:
    features = _state().runtime.list_features(model_id)
    return {
        "features": [f.model_dump(mode="json") for f in features],
        "ready": sum(1 for f in features if f.validated),
        "total": len(features),
    }


@router.post("/feature-semantics", status_code=201)
def create_feature(body: FeatureBody) -> dict[str, Any]:
    """Create a feature definition and validate it immediately.

    Validation runs on write rather than on use, because the alternative is discovering that
    a neutral value is undefined after paying for model calls.
    """
    moment = _now()
    try:
        feature = FeatureSemantics(
            id=random_id(FEATURE_PREFIX),
            created_at=moment,
            updated_at=moment,
            **body.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    problems = validate_feature(feature)
    feature = feature.model_copy(
        update={"validated": not problems, "validation_errors": problems}
    )
    _state().runtime.save_feature(feature)
    return feature.model_dump(mode="json")


@router.get("/feature-semantics/{feature_id}")
def get_feature(feature_id: str) -> dict[str, Any]:
    feature = _state().runtime.get_feature(feature_id)
    if feature is None:
        raise HTTPException(status_code=404, detail=f"no feature {feature_id}")
    payload = feature.model_dump(mode="json")
    payload["resolved_neutral"] = (
        resolve_neutral(feature) if feature.validated else None
    )
    return payload


@router.patch("/feature-semantics/{feature_id}")
def update_feature(feature_id: str, body: FeatureBody) -> dict[str, Any]:
    """Revising a neutral value is a scientific act, so it takes a new version."""
    runtime = _state().runtime
    feature = runtime.get_feature(feature_id)
    if feature is None:
        raise HTTPException(status_code=404, detail=f"no feature {feature_id}")
    try:
        updated = FeatureSemantics(
            id=feature.id,
            created_at=feature.created_at,
            updated_at=_now(),
            configuration_version=feature.configuration_version + 1,
            **body.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    problems = validate_feature(updated)
    updated = updated.model_copy(
        update={"validated": not problems, "validation_errors": problems}
    )
    runtime.save_feature(updated)
    return updated.model_dump(mode="json")


@router.delete("/feature-semantics/{feature_id}", status_code=204)
def delete_feature(feature_id: str) -> None:
    if not _state().runtime.delete_feature(feature_id):
        raise HTTPException(status_code=404, detail=f"no feature {feature_id}")


@router.post("/feature-semantics/{feature_id}/validate")
def revalidate_feature(feature_id: str) -> dict[str, Any]:
    """Re-run validation and report every problem, not just the first."""
    runtime = _state().runtime
    feature = runtime.get_feature(feature_id)
    if feature is None:
        raise HTTPException(status_code=404, detail=f"no feature {feature_id}")

    problems = validate_feature(feature)
    updated = feature.model_copy(
        update={"validated": not problems, "validation_errors": problems, "updated_at": _now()}
    )
    runtime.save_feature(updated)
    return {
        "feature_id": feature_id,
        "testable": not problems,
        "problems": problems,
        "resolved_neutral": resolve_neutral(updated) if not problems else None,
        "reason": None
        if not problems
        else "No valid neutral intervention is defined, so this feature cannot be tested. "
        "Ariadne will not manufacture a verdict for it.",
    }


# --------------------------------------------------------------------------------------
# Explanation sources and ingestion
# --------------------------------------------------------------------------------------


@router.get("/explanation-sources")
def list_sources() -> dict[str, Any]:
    sources = _state().runtime.list_explanation_sources()
    return {
        "sources": [s.model_dump(mode="json") for s in sources],
        "total": len(sources),
    }


@router.post("/explanation-sources", status_code=201)
def create_source(body: ExplanationSourceBody) -> dict[str, Any]:
    moment = _now()
    try:
        source = ExplanationSource(
            id=random_id(EXPLANATION_SOURCE_PREFIX),
            created_at=moment,
            updated_at=moment,
            **body.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _state().runtime.save_explanation_source(source)
    return source.model_dump(mode="json")


@router.get("/explanation-sources/{source_id}")
def get_source(source_id: str) -> dict[str, Any]:
    source = _state().runtime.get_explanation_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"no explanation source {source_id}")
    return source.model_dump(mode="json")


@router.delete("/explanation-sources/{source_id}", status_code=204)
def delete_source(source_id: str) -> None:
    if not _state().runtime.delete_explanation_source(source_id):
        raise HTTPException(status_code=404, detail=f"no explanation source {source_id}")


@router.post("/explanation-sources/{source_id}/ingest", status_code=202)
async def ingest_explanation(source_id: str, body: IngestBody) -> dict[str, Any]:
    """The real ingestion path: an explanation arrives and the claim lifecycle begins.

    Stored verbatim before anything interprets it. The claim compiled from an explanation is
    an interpretation, and an interpretation whose source has been discarded cannot be
    audited or re-compiled when the compiler improves.

    Then it is published as `EXPLANATION_RECEIVED` on the same bus the registry and drift
    events use, so ingestion is not a second pipeline running beside the real one.
    """
    from backend.runtime.worker import emit_explanation_received

    current = _state()
    source = current.runtime.get_explanation_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"no explanation source {source_id}")
    if not source.enabled:
        raise HTTPException(status_code=409, detail=f"source {source_id} is disabled")

    moment = _now()
    received = ReceivedExplanation(
        id=random_id(EXPLANATION_PREFIX),
        source_id=source.id,
        model_id=source.model_id,
        model_version=body.model_version,
        distribution_version=body.distribution_version,
        prediction_id=body.prediction_id,
        decision=body.decision,
        explanation=body.explanation,
        received_at=moment,
    )
    current.runtime.save_explanation(received)
    current.runtime.save_explanation_source(
        source.model_copy(
            update={
                "received_count": source.received_count + 1,
                "last_received_at": moment,
                "updated_at": moment,
            }
        )
    )

    event = emit_explanation_received(
        model_id=source.model_id,
        model_version=body.model_version,
        distribution_version=body.distribution_version,
        decision=body.decision,
        explanation=body.explanation,
        occurred_at=moment,
    )
    await current.bus.publish(event)
    current.note("event", "EXPLANATION_RECEIVED", event_id=event.event_id)

    return {
        "accepted": True,
        "explanation_id": received.id,
        "event_id": event.event_id,
        "note": "stored verbatim and queued; the worker will compile the claim",
    }


@router.get("/explanations")
def list_explanations(model_id: str | None = Query(default=None)) -> dict[str, Any]:
    items = _state().runtime.list_explanations(model_id)
    return {
        "explanations": [e.model_dump(mode="json") for e in items],
        "total": len(items),
    }
