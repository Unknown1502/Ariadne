"""Which model does an experiment actually run against?

The runner previously resolved a target with `(version, distribution)` and no identity at
all, which meant it could not distinguish one organisation's model from the built-in
laboratory even in principle. For a single-model demo that was invisible. The moment models
became a registered resource it became the most dangerous defect available to this system.

Not a wrong verdict - a *confident verdict about the wrong model*. An organisation registers
their endpoint, an event arrives, the experiment runs against the laboratory instead, and
evidence is recorded and scoped as though it described their model. Lineage, re-audit and
the append-only ledger would then all faithfully preserve a measurement of something else,
and nothing downstream could detect it, because every one of those mechanisms trusts the
scope it was handed.

So this module has one rule, and it is a refusal:

    **Never substitute.** If the requested model cannot be reached exactly as configured,
    raise. An experiment that does not run costs an event. An experiment that runs against
    the wrong model costs the integrity of every verdict derived from it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.core.configuration import TransportKind
from backend.core.errors import ValidationError
from backend.experiment_engine.adapters import RawPrediction
from backend.experiment_engine.target_model import (
    MODEL_ID as LABORATORY_MODEL_ID,
)
from backend.experiment_engine.target_model import (
    TargetModel,
    get_target_model,
)


def resolve_target_model(
    model_id: str,
    model_version: str,
    distribution_version: str,
    *,
    runtime: Any,
) -> TargetModel:
    """The model named by `model_id`, or a refusal explaining why it cannot be reached."""
    if model_id == LABORATORY_MODEL_ID:
        # The built-in laboratory. Real, local, and labelled as such everywhere it appears.
        return get_target_model(model_version, distribution_version)

    registered = _find(runtime, model_id)
    if registered is None:
        raise ValidationError(
            f"model {model_id!r} is not registered. Register it and complete its readiness "
            f"checks before events for it can be investigated — Ariadne will not fall back "
            f"to a different model."
        )

    if not registered.connection_id:
        raise ValidationError(
            f"model {model_id!r} has no model-endpoint connection, so there is nothing to "
            f"probe. Attach a connection and test it."
        )

    connection = runtime.get_connection(registered.connection_id)
    if connection is None:
        raise ValidationError(
            f"model {model_id!r} references connection {registered.connection_id}, which no "
            f"longer exists."
        )
    if not connection.is_live:
        raise ValidationError(
            f"the connection for {model_id!r} is not live (status {connection.status}). "
            f"Run its connection test — an experiment against an unverified endpoint would "
            f"produce evidence nobody can trust."
        )

    return _build_remote(registered, connection, model_version, distribution_version, runtime)


def _find(runtime: Any, model_id: str) -> Any:
    for candidate in runtime.list_models():
        if candidate.model_id == model_id:
            return candidate
    return None


def _build_remote(
    registered: Any,
    connection: Any,
    model_version: str,
    distribution_version: str,
    runtime: Any,
) -> TargetModel:
    """Assemble the adapter stack for a registered model.

    The feature space comes from the operator's declared semantics rather than from the
    laboratory's, because that declaration is the whole point: "neutralize X" has no meaning
    without a neutral value somebody is prepared to defend. A model whose features are not
    validated is refused here rather than probed with a guess.
    """
    from backend.experiment_engine.adapters import (
        HttpTransport,
        ModelIdentity,
        build_remote_model,
    )
    from backend.experiment_engine.distributions import FeatureSpec

    features = [f for f in runtime.list_features(registered.model_id) if f.validated]
    if not features:
        raise ValidationError(
            f"model {registered.model_id!r} has no validated feature semantics. Ariadne "
            f"supplies the verification protocol; the neutral value for each feature is a "
            f"domain judgement it cannot infer."
        )

    space = {
        feature.name: FeatureSpec(
            name=feature.name,
            minimum=feature.minimum if feature.minimum is not None else 0.0,
            maximum=feature.maximum if feature.maximum is not None else 1.0,
            neutral_value=(
                feature.neutral_value
                if feature.neutral_value is not None
                else ((feature.minimum or 0.0) + (feature.maximum or 1.0)) / 2.0
            ),
            description=feature.description or feature.name,
        )
        for feature in features
    }

    if connection.transport is TransportKind.VERTEX_AI:
        from backend.experiment_engine.gemini_target import build_gemini_target

        model, _ = build_gemini_target(
            project=connection.project,
            location=connection.region or "global",
            gemini_model=connection.model_id or "gemini-3.5-flash",
            scope_version=model_version,
            distribution_version=distribution_version,
            feature_space=space,
        )
        return model

    if connection.transport is TransportKind.HTTP:
        return build_remote_model(
            identity=ModelIdentity(
                model_id=registered.model_id,
                version=model_version,
                distribution_version=distribution_version,
                # Assumed non-deterministic until measured. `measure_noise_floor` is how an
                # integrator earns the cheaper path; assuming determinism would let caching
                # report perfect stability for a model that has none.
                deterministic=False,
            ),
            codec=_PathCodec(registered.output),
            transport=HttpTransport(url=connection.endpoint),
            feature_space=space,
            # A budget is mandatory, not optional. An experiment whose sample size is
            # bounded by nothing is an experiment that can spend without limit against
            # somebody else's endpoint, and BudgetedTargetModel fails closed rather than
            # silently running fewer cases than the plan declared.
            max_calls=_DEFAULT_MAX_CALLS,
            # The transport is deliberately thin and takes its timeout per call, so the
            # connection's configured value is applied here rather than baked into it.
            timeout_seconds=connection.timeout_seconds,
        )

    raise ValidationError(
        f"transport {connection.transport} cannot be used as a target model endpoint. "
        f"Supported: VERTEX_AI, HTTP."
    )


class _PathCodec:
    """Encode features to a request and decode a response using the declared output paths.

    The contract the operator validated during onboarding is the contract used here, so a
    response shape that passed validation is the shape this reads. Nothing is guessed.
    """

    def __init__(self, output: Any) -> None:
        self._output = output

    def encode(self, features: Mapping[str, float]) -> dict[str, Any]:
        return {"features": {name: float(value) for name, value in features.items()}}

    def decode(self, payload: Any) -> RawPrediction:

        score = _walk(payload, self._output.score_path)
        if score is _MISSING:
            raise ValidationError(
                f"no value at {self._output.score_path!r} in the model response. The output "
                f"contract was validated against a different shape."
            )
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValidationError(
                f"{self._output.score_path!r} holds {type(score).__name__}, not a number. "
                f"The protocol measures how far a decision moved, so a class label leaves "
                f"nothing to measure."
            )
        decision = _walk(payload, self._output.decision_path)
        explanation = _walk(payload, self._output.explanation_path)
        return RawPrediction(
            score=float(score),
            decision="UNKNOWN" if decision is _MISSING else str(decision),
            explanation="" if explanation is _MISSING else str(explanation),
        )


_DEFAULT_MAX_CALLS = 1000
"""Ceiling on model calls per experiment for a registered endpoint.

Deliberately present rather than unbounded: the three arms plus replicates multiply quickly,
and an integrator discovering the cost of an audit from their bill is the outcome
`BudgetedTargetModel` exists to prevent."""

_MISSING = object()


def _walk(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current
