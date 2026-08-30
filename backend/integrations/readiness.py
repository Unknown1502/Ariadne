"""Can this model actually be verified, and if not, exactly what is missing?

The keystone of onboarding. Every other configuration resource answers a narrow question -
is this endpoint reachable, is this neutral value defensible, is this source registered -
and none of them answers the one a governance engineer actually has: *am I done?*

Two decisions make this honest rather than decorative.

**It reads live state, never a stored flag.** `RegisteredModel.status` is a cache of the
last answer, not the answer. A model whose endpoint fails at 3am stops being ready the next
time anyone asks, because the check re-derives from the connection, the features, and the
source every time. A readiness field that only changes when someone edits a form is a field
that lies within a day.

**Every failure carries an imperative.** A gate that says NOT READY without saying what to
do is a dead end rather than a diagnosis, and the blocker text is what the console renders,
so it is written for the person who has to clear it.

What this deliberately does *not* do is call the model. Reachability is the connection's
job and it has its own probe with its own recorded checks; duplicating that here would mean
two components disagreeing about whether an endpoint is up.
"""

from __future__ import annotations

from typing import Any

from backend.core.clock import Clock, SystemClock
from backend.core.configuration import (
    ConnectionKind,
    ModelStatus,
    Readiness,
    ReadinessCheck,
    RegisteredModel,
)


def evaluate(
    model: RegisteredModel, runtime: Any, *, clock: Clock | None = None
) -> Readiness:
    """Run every onboarding gate against current persisted state."""
    now = (clock or SystemClock()).now()
    checks: list[ReadinessCheck] = [
        _model_endpoint(model, runtime),
        _output_contract(model),
        _explanation_source(model, runtime),
        _feature_semantics(model, runtime),
        _lifecycle_events(runtime),
        _evidence_store(runtime),
    ]
    ready = all(check.passed for check in checks)
    return Readiness(
        model_id=model.model_id,
        ready=ready,
        status="READY_FOR_VERIFICATION" if ready else "NOT_READY",
        checks=checks,
        checked_at=now,
    )


# -- gates ------------------------------------------------------------------------------


def _model_endpoint(model: RegisteredModel, runtime: Any) -> ReadinessCheck:
    if not model.connection_id:
        return ReadinessCheck(
            name="MODEL_ENDPOINT",
            passed=False,
            detail="no connection is attached to this model",
            blocker="Attach a model-endpoint connection, then run Test connection on it.",
        )
    connection = runtime.get_connection(model.connection_id)
    if connection is None:
        return ReadinessCheck(
            name="MODEL_ENDPOINT",
            passed=False,
            detail=f"connection {model.connection_id} no longer exists",
            blocker="Attach an existing connection, or create one and test it.",
        )
    if not connection.is_live:
        return ReadinessCheck(
            name="MODEL_ENDPOINT",
            passed=False,
            detail=f"connection {connection.name!r} is {connection.status}",
            blocker=(
                "Run Test connection. A connection is only live once a real check has "
                "succeeded — creating one leaves it not configured."
            ),
        )
    return ReadinessCheck(
        name="MODEL_ENDPOINT",
        passed=True,
        detail=f"{connection.name} · {connection.transport} · last success "
        f"{connection.last_success_at.isoformat() if connection.last_success_at else 'unknown'}",
    )


def _output_contract(model: RegisteredModel) -> ReadinessCheck:
    """Declared paths are not enough; they have to have matched a real response.

    A path that is merely plausible is a path that fails on the first live call, and it
    fails inside an experiment that has already cost money.
    """
    contract = model.output
    if not contract.score_path:
        return ReadinessCheck(
            name="OUTPUT_CONTRACT",
            passed=False,
            detail="no score path declared",
            blocker="Declare where the continuous score sits in the model's response.",
        )
    if not contract.validated_against:
        return ReadinessCheck(
            name="OUTPUT_CONTRACT",
            passed=False,
            detail=f"paths declared ({contract.score_path}) but never checked against a response",
            blocker=(
                "Validate the output contract against a real model response. Declaring a "
                "path is not the same as finding a value at it."
            ),
        )
    return ReadinessCheck(
        name="OUTPUT_CONTRACT",
        passed=True,
        detail=(
            f"score at {contract.score_path}, explanation at {contract.explanation_path}, "
            f"validated against a real response"
        ),
    )


def _explanation_source(model: RegisteredModel, runtime: Any) -> ReadinessCheck:
    sources = [
        source
        for source in runtime.list_explanation_sources()
        if source.model_id == model.model_id
    ]
    enabled = [source for source in sources if source.enabled]
    if not sources:
        return ReadinessCheck(
            name="EXPLANATION_SOURCE",
            passed=False,
            detail="no explanation source registered for this model",
            blocker=(
                "Register where explanations arrive from — the model's own response, a "
                "separate endpoint, or an external event."
            ),
        )
    if not enabled:
        return ReadinessCheck(
            name="EXPLANATION_SOURCE",
            passed=False,
            detail=f"{len(sources)} source(s) registered, all disabled",
            blocker="Enable an explanation source.",
        )
    received = sum(source.received_count for source in enabled)
    return ReadinessCheck(
        name="EXPLANATION_SOURCE",
        passed=True,
        detail=f"{len(enabled)} enabled · {received} explanation(s) received so far",
    )


def _feature_semantics(model: RegisteredModel, runtime: Any) -> ReadinessCheck:
    """The half Ariadne cannot supply.

    Neutralizing a feature only means something where a defensible neutral value exists,
    and no adapter invents one. A model with no validated feature has nothing that can be
    intervened on, which makes every claim about it untestable in principle.
    """
    features = runtime.list_features(model.model_id)
    if not features:
        return ReadinessCheck(
            name="FEATURE_SEMANTICS",
            passed=False,
            detail="no feature semantics declared for this model",
            blocker=(
                "Declare what neutralizing a feature means in your domain. Ariadne supplies "
                "the verification protocol; the neutral value is yours to defend."
            ),
        )
    unvalidated = [feature for feature in features if not feature.validated]
    if not any(feature.validated for feature in features):
        return ReadinessCheck(
            name="FEATURE_SEMANTICS",
            passed=False,
            detail=f"{len(features)} declared, none testable",
            blocker=(
                "Fix the validation errors on at least one feature: "
                + "; ".join(unvalidated[0].validation_errors[:2])
            ),
        )
    ready = [feature for feature in features if feature.validated]
    detail = f"{len(ready)} of {len(features)} features are intervention-ready"
    if unvalidated:
        detail += f" ({', '.join(f.name for f in unvalidated[:3])} not testable)"
    return ReadinessCheck(name="FEATURE_SEMANTICS", passed=True, detail=detail)


def _lifecycle_events(runtime: Any) -> ReadinessCheck:
    """Without a lifecycle source, evidence is never re-tested when the model changes.

    Not fatal to a single verification, and fatal to the product's actual claim: an
    explanation verified once and never re-checked is exactly the stale assurance Ariadne
    exists to prevent.
    """
    lifecycle = [
        connection
        for connection in runtime.list_connections()
        if connection.kind in (ConnectionKind.MODEL_REGISTRY, ConnectionKind.DRIFT_MONITOR)
    ]
    live = [connection for connection in lifecycle if connection.is_live]
    if not lifecycle:
        return ReadinessCheck(
            name="LIFECYCLE_EVENTS",
            passed=False,
            detail="no model-registry or drift-monitor connection",
            blocker=(
                "Connect a model registry or drift monitor. Without one, evidence is never "
                "re-tested when the model or its data changes."
            ),
        )
    if not live:
        return ReadinessCheck(
            name="LIFECYCLE_EVENTS",
            passed=False,
            detail=f"{len(lifecycle)} lifecycle connection(s), none live",
            blocker="Run Test connection on the registry or drift-monitor connection.",
        )
    kinds = sorted({str(connection.kind) for connection in live})
    return ReadinessCheck(
        name="LIFECYCLE_EVENTS", passed=True, detail=f"live: {', '.join(kinds)}"
    )


def _evidence_store(runtime: Any) -> ReadinessCheck:
    """The ledger has to be writable, or a verdict has nowhere to be recorded."""
    try:
        stats = runtime.stats()
    except Exception as exc:  # noqa: BLE001 - an unreachable store is the finding
        return ReadinessCheck(
            name="EVIDENCE_STORE",
            passed=False,
            detail=f"runtime store unreachable: {type(exc).__name__}: {exc}",
            blocker="Check the runtime store configuration and credentials.",
        )
    return ReadinessCheck(
        name="EVIDENCE_STORE",
        passed=True,
        detail=f"reachable · {stats.get('investigations', 0)} investigation record(s)",
    )


def apply_readiness(model: RegisteredModel, readiness: Readiness) -> RegisteredModel:
    """Cache the last answer on the model, without letting the cache become the answer."""
    if model.status is ModelStatus.DISABLED:
        return model
    return model.model_copy(
        update={
            "status": ModelStatus.READY if readiness.ready else ModelStatus.CONFIGURING,
            "updated_at": readiness.checked_at,
        }
    )
