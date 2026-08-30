"""Registered models, output contracts, and readiness.

The onboarding spine. `configuration_routes` owns the individual resources an organisation
supplies - connections, feature semantics, explanation sources - and this owns the model
those resources are *for*, plus the one question none of them answers on its own:

    am I done, and if not, what exactly is missing?

Two decisions keep it honest.

**Readiness reads live state, never a stored flag.** `RegisteredModel.status` caches the
last answer; it is not the answer. A model whose endpoint fails overnight stops being ready
the next time anyone asks, because the check re-derives from the connection, the features
and the source every time. A readiness field that only changes when someone edits a form is
a field that lies within a day.

**An output contract is not validated by being declared.** A path that is merely plausible
fails on the first live call, inside an experiment that has already cost money - so the
paths have to be checked against a real response before the model can be called ready.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.api.authz import CanConfigure, CanRead
from backend.core.configuration import (
    MODEL_PREFIX,
    ModelStatus,
    OutputContract,
    RegisteredModel,
)
from backend.core.ids import random_id
from backend.integrations.readiness import apply_readiness, evaluate

router = APIRouter(prefix="/api/v1", tags=["models"])

_MISSING = object()


def _now() -> datetime:
    return datetime.now(UTC)


def _state() -> Any:
    from backend.api.main import app_state

    return app_state()


def _resolve_path(payload: Any, path: str) -> Any:
    """Walk a dot path. Returns a sentinel rather than raising, so every miss is reported
    rather than the first one aborting the check."""
    current = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


class ModelBody(BaseModel):
    model_id: str = Field(min_length=1, max_length=120)
    """The organisation's own identifier, the one their events carry."""

    name: str = Field(min_length=1, max_length=160)
    provider: str = ""
    connection_id: str = ""
    current_version: str = ""
    distribution_version: str = ""

    model_config = {"protected_namespaces": ()}


class OutputContractBody(BaseModel):
    score_path: str = Field(default="score", min_length=1)
    decision_path: str = "decision"
    explanation_path: str = "explanation"

    model_config = {"protected_namespaces": ()}


class ValidateOutputBody(BaseModel):
    sample_response: dict[str, Any]

    model_config = {"protected_namespaces": ()}


# -- registration ----------------------------------------------------------------------


@router.get("/registered-models")
def list_models(_: CanRead) -> dict[str, Any]:
    models = _state().runtime.list_models()
    return {
        "models": [model.model_dump(mode="json") for model in models],
        "ready": sum(1 for model in models if model.status is ModelStatus.READY),
        "total": len(models),
    }


@router.post("/registered-models", status_code=201)
def register_model(body: ModelBody, _: CanConfigure) -> dict[str, Any]:
    """Register a model. It is CONFIGURING until every readiness gate passes."""
    runtime = _state().runtime
    if any(model.model_id == body.model_id for model in runtime.list_models()):
        raise HTTPException(
            status_code=409, detail=f"model {body.model_id!r} is already registered"
        )
    moment = _now()
    try:
        model = RegisteredModel(
            id=random_id(MODEL_PREFIX),
            created_at=moment,
            updated_at=moment,
            **body.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    runtime.save_model(model)
    return model.model_dump(mode="json")


@router.get("/registered-models/{registered_id}")
def get_model(registered_id: str, _: CanRead) -> dict[str, Any]:
    model = _state().runtime.get_model(registered_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"no registered model {registered_id}")
    return model.model_dump(mode="json")


@router.delete("/registered-models/{registered_id}", status_code=204)
def delete_model(registered_id: str, _: CanConfigure) -> None:
    if not _state().runtime.delete_model(registered_id):
        raise HTTPException(status_code=404, detail=f"no registered model {registered_id}")


# -- output contract ---------------------------------------------------------------------


@router.patch("/registered-models/{registered_id}/output")
def declare_output(
    registered_id: str, body: OutputContractBody, _: CanConfigure
) -> dict[str, Any]:
    """Declare the paths. Declaring is not validating - that needs a real response."""
    runtime = _state().runtime
    model = runtime.get_model(registered_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"no registered model {registered_id}")
    updated = model.model_copy(
        update={"output": OutputContract(**body.model_dump()), "updated_at": _now()}
    )
    runtime.save_model(updated)
    return updated.model_dump(mode="json")


@router.post("/registered-models/{registered_id}/output/validate")
def validate_output(
    registered_id: str, body: ValidateOutputBody, _: CanConfigure
) -> dict[str, Any]:
    """Check the declared paths against a real response, reporting each one.

    The score check is stricter than the others on purpose. The protocol measures *how far*
    a decision moved, so a score that turns out to be a class label would collapse every
    delta to nothing or to a full unit, and reproducibility would become noise. Catching
    that here costs one request; catching it later costs an experiment.
    """
    runtime = _state().runtime
    model = runtime.get_model(registered_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"no registered model {registered_id}")

    contract = model.output
    checks: list[dict[str, Any]] = []

    score = _resolve_path(body.sample_response, contract.score_path)
    if score is _MISSING:
        checks.append(
            {
                "name": "score",
                "passed": False,
                "detail": f"nothing found at {contract.score_path!r}",
            }
        )
    elif isinstance(score, bool) or not isinstance(score, (int, float)):
        checks.append(
            {
                "name": "score",
                "passed": False,
                "detail": (
                    f"{contract.score_path!r} holds {type(score).__name__}, not a number. "
                    "The protocol measures how far a decision moved, so a class label "
                    "leaves nothing to measure."
                ),
            }
        )
    else:
        checks.append(
            {"name": "score", "passed": True, "detail": f"continuous score found: {score}"}
        )

    for label, path in (
        ("explanation", contract.explanation_path),
        ("decision", contract.decision_path),
    ):
        value = _resolve_path(body.sample_response, path)
        if value is _MISSING:
            checks.append(
                {"name": label, "passed": False, "detail": f"nothing found at {path!r}"}
            )
        elif not str(value).strip():
            checks.append({"name": label, "passed": False, "detail": f"{path!r} is empty"})
        else:
            checks.append(
                {"name": label, "passed": True, "detail": f"found: {str(value)[:60]}"}
            )

    ok = all(check["passed"] for check in checks)
    if ok:
        runtime.save_model(
            model.model_copy(
                update={
                    "output": contract.model_copy(
                        update={
                            "validated_against": json.dumps(body.sample_response)[:400]
                        }
                    ),
                    "updated_at": _now(),
                }
            )
        )
    return {"ok": ok, "checks": checks}


# -- readiness ---------------------------------------------------------------------------


@router.get("/registered-models/{registered_id}/readiness")
def model_readiness(registered_id: str, _: CanRead) -> dict[str, Any]:
    """Every onboarding gate, re-derived from live state, with what to do about each."""
    runtime = _state().runtime
    model = runtime.get_model(registered_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"no registered model {registered_id}")

    readiness = evaluate(model, runtime)
    runtime.save_model(apply_readiness(model, readiness))
    payload = readiness.model_dump(mode="json")
    payload["blockers"] = readiness.blockers
    return payload
