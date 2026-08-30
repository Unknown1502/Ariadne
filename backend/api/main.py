"""The Ariadne API.

Read-only over the evidence ledger, plus a small set of endpoints that *emit events*.

That split is deliberate and worth stating plainly: the console cannot cause a verdict. It
can publish a MODEL_VERSION_DEPLOYED event, exactly as a model registry would, and then it
watches. Everything it displays is read back from the ledger, so the UI is a view of the
record rather than a source of truth - and nothing it shows can be true only on screen.

The endpoints are shaped around the investigation narrative the console tells:

    decision -> explanation -> claim -> experiment -> evidence -> verdict -> action
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.agents.registry import AgentRegistry
from backend.config import get_settings
from backend.core.clock import SystemClock
from backend.core.enums import LineageRelation
from backend.core.errors import AriadneError
from backend.debt.calculator import explain
from backend.events.bus import build_event_bus
from backend.experiment_engine.target_model import (
    KNOWN_VERSIONS,
    MODEL_ID,
    SYNTHETIC_DISCLAIMER,
    describe_version,
)
from backend.lineage.service import LineageService
from backend.observability.logging import configure_logging, get_logger
from backend.runtime.orchestrator import build_pipeline
from backend.runtime.worker import (
    AriadneWorker,
    emit_distribution_changed,
    emit_explanation_received,
    emit_model_version_deployed,
)
from backend.storage.runtime import open_runtime_store
from backend.storage.sql import open_ledger

logger = get_logger("ariadne.api")


class AppState:
    """Everything the API needs, wired once at startup."""

    def __init__(self) -> None:
        settings = get_settings()
        configure_logging(settings.log_level, settings.log_format)

        self.settings = settings
        self.clock = SystemClock()
        self.ledger = open_ledger()
        # Built from configuration, like the event bus below - constructing LocalRuntimeStore
        # directly here meant RUNTIME_STORE=firestore was silently ignored while
        # /api/v1/system reported "firestore" to the console. Every investigation,
        # idempotency claim, and experiment checkpoint was actually written to the
        # container's own ephemeral filesystem instead: durable-looking while the same
        # instance stayed warm, and gone the moment Cloud Run replaced it. Found by checking
        # a live deployment's data survived a scale-down/scale-up cycle, not by reading this
        # file - it looked identical to the correct version at a glance.
        self.runtime = open_runtime_store(clock=self.clock)
        self.lineage = LineageService(self.ledger, clock=self.clock)
        self.registry = AgentRegistry.with_defaults()
        self.pipeline = build_pipeline(
            ledger=self.ledger,
            runtime=self.runtime,
            clock=self.clock,
            default_repetitions=settings.default_repetitions,
            default_seed=settings.default_seed,
        )
        # Built from configuration, not hardcoded. Constructing LocalEventBus directly
        # here meant EVENT_BUS=pubsub was silently ignored while /api/v1/system reported
        # "pubsub" to the console - a false cloud-proof claim of exactly the kind this
        # system exists to catch.
        self.bus = build_event_bus(settings)
        self.worker = AriadneWorker(
            pipeline=self.pipeline,
            runtime=self.runtime,
            lineage=self.lineage,
            bus=self.bus,
            clock=self.clock,
        )
        self.bus.subscribe(self.worker.handle)
        self.activity: list[dict[str, Any]] = []

    def note(self, kind: str, detail: str, **fields: Any) -> None:
        """Record something that happened, for the console's agent timeline."""
        self.activity.append(
            {
                "at": self.clock.now().isoformat(),
                "kind": kind,
                "detail": detail,
                **fields,
            }
        )
        del self.activity[:-200]


state: AppState | None = None


def app_state() -> AppState:
    if state is None:  # pragma: no cover - only before startup
        raise HTTPException(status_code=503, detail="Ariadne is still starting up")
    return state


@asynccontextmanager
async def lifespan(app: FastAPI):
    global state
    state = AppState()
    state.bus.start()
    logger.info("ariadne api ready")
    yield
    await state.bus.stop()
    state.ledger.dispose()


app = FastAPI(
    title="Ariadne API",
    version="1.0.0",
    description="Executable Explanation Protocol. AI explanations that have to prove themselves.",
    lifespan=lifespan,
)

# Operator configuration: connections, feature semantics, explanation sources. Registered
# rather than inlined because it is a genuinely separate concern - configuring what Ariadne
# points at, versus reading what Ariadne concluded.
from backend.api.configuration_routes import router as configuration_router  # noqa: E402

app.include_router(configuration_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------------------
# Health and metadata
# --------------------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "ariadne", "version": "1.0.0"}


@app.get("/api/v1/system")
def system() -> dict[str, Any]:
    """What this deployment actually is. Read by the console's honesty banner."""
    current = app_state()
    settings = current.settings
    return {
        "disclaimer": SYNTHETIC_DISCLAIMER,
        "environment": settings.app_env,
        "reasoner": {
            "provider": settings.llm_provider,
            "model": (
                settings.gemini_model
                if settings.llm_provider == "gemini"
                else "offline-deterministic-reasoner/1.0.0"
            ),
            # Surfaced so the UI never shows a Gemini badge over the offline reasoner.
            "is_language_model": settings.llm_provider == "gemini",
        },
        "cloud": {
            "enabled": settings.enable_google_cloud,
            "event_bus": settings.event_bus,
            "runtime_store": settings.runtime_store,
            "database": "cloud-sql" if settings.database_url else "local-sqlite",
            "project": settings.gcp_project_id or None,
            "region": settings.gcp_region,
        },
        "policy_version": "1.0.0",
        "verifier_version": "1.0.0",
        "protocol_version": "1.0.0",
    }


@app.get("/api/v1/models/{model_id}")
def model_registry(model_id: str) -> dict[str, Any]:
    if model_id != MODEL_ID:
        raise HTTPException(status_code=404, detail=f"unknown model {model_id!r}")
    return {
        "model_id": MODEL_ID,
        "versions": [describe_version(v) for v in KNOWN_VERSIONS],
        "disclaimer": SYNTHETIC_DISCLAIMER,
    }


# --------------------------------------------------------------------------------------
# Investigations
# --------------------------------------------------------------------------------------


@app.get("/api/v1/investigations")
def list_investigations(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    current = app_state()
    rows = current.runtime.list_investigations()[:limit]
    return {
        "investigations": [
            {
                "id": i.id,
                "state": str(i.state),
                "model_version": i.scope.model_version,
                "distribution_version": i.scope.distribution_version,
                "trigger_event_type": i.trigger_event_type,
                "priority": i.priority,
                "verdict": _verdict_summary(current, i.verdict_id),
                "created_at": i.created_at.isoformat(),
                "updated_at": i.updated_at.isoformat(),
                "last_error": i.last_error,
            }
            for i in rows
        ]
    }


@app.get("/api/v1/investigations/{investigation_id}")
def get_investigation(investigation_id: str) -> dict[str, Any]:
    """The whole narrative for one investigation, in the order the console displays it."""
    current = app_state()
    investigation = current.runtime.get_investigation(investigation_id)
    if investigation is None:
        raise HTTPException(status_code=404, detail=f"unknown investigation {investigation_id!r}")

    claim = current.ledger.get_claim(investigation.claim_id or "")
    plan = current.ledger.get_plan(investigation.experiment_id or "")
    evidence = current.ledger.get_evidence(investigation.evidence_id or "")
    verdict = current.ledger.get_verdict(investigation.verdict_id or "")
    snapshot = current.ledger.get_debt_snapshot(investigation.debt_snapshot_id or "")
    decision = current.ledger.get_decision(investigation.decision_id or "")

    return {
        "investigation": {
            "id": investigation.id,
            "state": str(investigation.state),
            "priority": investigation.priority,
            "trigger_event_id": investigation.trigger_event_id,
            "trigger_event_type": investigation.trigger_event_type,
            "created_at": investigation.created_at.isoformat(),
            "updated_at": investigation.updated_at.isoformat(),
            "last_error": investigation.last_error,
            "scope": investigation.scope.model_dump(),
        },
        "decision": {
            "decision": investigation.source_decision,
            "explanation": investigation.source_explanation,
        },
        "claim": claim.model_dump(by_alias=True, mode="json") if claim else None,
        "experiment": plan.model_dump(mode="json") if plan else None,
        "evidence": evidence.model_dump(mode="json") if evidence else None,
        "verdict": verdict.model_dump(mode="json") if verdict else None,
        "debt": snapshot.model_dump(mode="json") if snapshot else None,
        "action": decision.model_dump(mode="json") if decision else None,
    }


# --------------------------------------------------------------------------------------
# Lineage and debt
# --------------------------------------------------------------------------------------


@app.get("/api/v1/lineage/{claim_family_id}")
def get_lineage(claim_family_id: str, at: str | None = None) -> dict[str, Any]:
    """A claim family's full history, plus what is current (optionally at a past moment)."""
    current = app_state()
    moment = _parse_moment(at)
    view = current.lineage.view(claim_family_id, at=moment)
    if not view.entries:
        raise HTTPException(status_code=404, detail=f"no lineage for {claim_family_id!r}")

    return {
        "claim_family_id": claim_family_id,
        "as_of": (moment or current.clock.now()).isoformat(),
        "current": view.current.model_dump(mode="json") if view.current else None,
        "statuses_by_version": {
            version: str(status) for version, status in view.statuses_by_version.items()
        },
        "expired_entry_ids": sorted(view.expired_entry_ids),
        "audit_priority": current.lineage.audit_priority(claim_family_id, at=moment),
        "chain_intact": current.lineage.verify_chain(claim_family_id) == [],
        "entries": [
            {
                **entry.model_dump(mode="json"),
                "is_expiry": entry.relation is LineageRelation.EXPIRES,
                "is_expired": entry.id in view.expired_entry_ids,
            }
            for entry in view.entries
        ],
    }


@app.get("/api/v1/claim-families")
def list_claim_families(model_id: str = MODEL_ID) -> dict[str, Any]:
    current = app_state()
    families = current.lineage.families_for_model(model_id)
    return {
        "families": [
            {
                "claim_family_id": family,
                "audit_priority": current.lineage.audit_priority(family),
                "statuses_by_version": {
                    v: str(s)
                    for v, s in current.lineage.view(family).statuses_by_version.items()
                },
            }
            for family in families
        ]
    }


@app.get("/api/v1/debt/{model_id}")
def get_debt(model_id: str, limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    current = app_state()
    history = current.ledger.debt_history(model_id, limit=limit)
    if not history:
        return {"model_id": model_id, "current": None, "history": [], "rendered": None}
    latest = history[0]
    return {
        "model_id": model_id,
        "current": latest.model_dump(mode="json"),
        "delta": latest.delta,
        "rendered": explain(latest),
        "history": [
            {
                "id": s.id,
                "total": s.total,
                "computed_at": s.computed_at.isoformat(),
                "policy_version": s.policy_version,
            }
            for s in reversed(history)
        ],
    }


# --------------------------------------------------------------------------------------
# Fleet and runtime proof
# --------------------------------------------------------------------------------------


@app.get("/api/v1/fleet")
def fleet() -> dict[str, Any]:
    current = app_state()
    return {"agents": current.registry.describe()}


@app.get("/api/v1/runtime")
def runtime_state() -> dict[str, Any]:
    """Evidence that the asynchronous machinery is real, not staged."""
    current = app_state()
    return {
        "bus": current.bus.snapshot(),
        "worker": {
            "worker_id": current.worker.worker_id,
            "events_seen": current.worker.stats.events_seen,
            "events_processed": current.worker.stats.events_processed,
            "duplicates_skipped": current.worker.stats.duplicates_skipped,
            "investigations_started": current.worker.stats.investigations_started,
            "failures": current.worker.stats.failures,
            "handled_types": current.worker.stats.handled_types,
        },
        "checkpoints": current.runtime.stats(),
        "ledger": current.ledger.counts(),
        "dead_letters": [
            {
                "event_id": dl.event.event_id,
                "event_type": str(dl.event.event_type),
                "error_code": dl.error_code,
                "attempts": dl.attempts,
            }
            for dl in current.bus.dead_letters
        ],
        "scheduled_audits": [
            {
                "id": a.id,
                "claim_family_id": a.claim_family_id,
                "scheduled_for": a.scheduled_for.isoformat(),
                "priority": a.priority,
                "reason_code": a.reason_code,
                "executed": not a.is_pending,
            }
            for a in current.runtime.all_audits()
        ],
        "integrity": {
            "lineage_chain_broken_rows": current.ledger.verify_integrity("lineage_entries"),
            "verdict_rows_broken": current.ledger.verify_integrity("verdicts"),
        },
        "activity": current.activity[-50:],
    }


@app.get("/api/v1/approvals")
def approvals() -> dict[str, Any]:
    current = app_state()
    return {
        "pending": [r.model_dump(mode="json") for r in current.runtime.pending_approvals()]
    }


class ApprovalDecision(BaseModel):
    approve: bool
    decided_by: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=1000)


@app.post("/api/v1/approvals/{approval_id}/decide")
def decide_approval(approval_id: str, body: ApprovalDecision) -> dict[str, Any]:
    """Record a human decision on a high-impact action."""
    current = app_state()
    request = current.runtime.get_approval(approval_id)
    if request is None:
        raise HTTPException(status_code=404, detail=f"unknown approval {approval_id!r}")
    if request.status != "PENDING":
        raise HTTPException(status_code=409, detail=f"approval is already {request.status}")

    resolved = request.model_copy(
        update={
            "status": "APPROVED" if body.approve else "REJECTED",
            "decided_at": current.clock.now(),
            "decided_by": body.decided_by,
            "decision_note": body.note,
        }
    )
    current.runtime.save_approval(resolved)
    current.note("approval", f"{resolved.status} by {body.decided_by}", action=str(request.action))
    return resolved.model_dump(mode="json")


# --------------------------------------------------------------------------------------
# Event emission
# --------------------------------------------------------------------------------------


class ModelDeployBody(BaseModel):
    model_config = {"protected_namespaces": ()}

    model_id: str = MODEL_ID
    model_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    """Validated at the request boundary so a malformed version returns 422 rather than
    surfacing a domain error from deep in the event constructor."""

    distribution_version: str = Field(
        default="baseline_2024.1", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
    )
    deployed_by: str | None = None
    duplicate: bool = False
    """Publish the event twice, on purpose. The demo uses this to show that an
    at-least-once bus cannot produce a duplicate audit."""


@app.post("/api/v1/events/model-version-deployed")
async def publish_model_deployed(body: ModelDeployBody) -> dict[str, Any]:
    """Emit the event that wakes Ariadne up.

    Returns as soon as the event is queued. No verdict is computed here - the worker picks
    it up asynchronously, which is exactly the behaviour the demo is claiming.
    """
    current = app_state()
    event = emit_model_version_deployed(
        model_id=body.model_id,
        model_version=body.model_version,
        distribution_version=body.distribution_version,
        occurred_at=datetime.now(UTC),
        deployed_by=body.deployed_by,
    )
    await current.bus.publish(event)
    if body.duplicate:
        await current.bus.publish_duplicate(event)
    current.note(
        "event",
        f"MODEL_VERSION_DEPLOYED v{body.model_version}",
        event_id=event.event_id,
        idempotency_key=event.idempotency_key,
    )
    return {
        "accepted": True,
        "event_id": event.event_id,
        "idempotency_key": event.idempotency_key,
        "note": "queued; the worker will pick this up asynchronously",
    }


class DistributionChangeBody(BaseModel):
    model_config = {"protected_namespaces": ()}

    model_id: str = MODEL_ID
    distribution_version: str
    previous_distribution_version: str | None = "baseline_2024.1"
    drift_score: float = Field(default=0.0, ge=0.0, le=1.0)
    affected_features: list[str] = Field(default_factory=list)


@app.post("/api/v1/events/distribution-changed")
async def publish_distribution_changed(body: DistributionChangeBody) -> dict[str, Any]:
    current = app_state()
    event = emit_distribution_changed(
        model_id=body.model_id,
        distribution_version=body.distribution_version,
        previous_distribution_version=body.previous_distribution_version,
        occurred_at=datetime.now(UTC),
        drift_score=body.drift_score,
        affected_features=body.affected_features,
    )
    await current.bus.publish(event)
    current.note("event", f"DISTRIBUTION_CHANGED -> {body.distribution_version}",
                 event_id=event.event_id)
    return {"accepted": True, "event_id": event.event_id}


class ExplanationBody(BaseModel):
    model_config = {"protected_namespaces": ()}

    model_id: str = MODEL_ID
    model_version: str = "1.0.0"
    distribution_version: str = "baseline_2024.1"
    decision: str = "HIGH_PRIORITY"
    explanation: str = Field(min_length=1, max_length=4000)
    case_id: str | None = None


@app.post("/api/v1/events/explanation-received")
async def publish_explanation(body: ExplanationBody) -> dict[str, Any]:
    current = app_state()
    event = emit_explanation_received(
        model_id=body.model_id,
        model_version=body.model_version,
        distribution_version=body.distribution_version,
        decision=body.decision,
        explanation=body.explanation,
        occurred_at=datetime.now(UTC),
        case_id=body.case_id,
    )
    await current.bus.publish(event)
    current.note("event", "EXPLANATION_RECEIVED", event_id=event.event_id)
    return {"accepted": True, "event_id": event.event_id}


# --------------------------------------------------------------------------------------
# Live activity
# --------------------------------------------------------------------------------------


@app.get("/api/v1/stream")
async def stream() -> StreamingResponse:
    """Server-sent events carrying real runtime state.

    Emits an actual snapshot of the bus, the worker, and the ledger on each tick. Nothing
    here is simulated activity - if the numbers do not move, nothing is happening.
    """

    async def generate():
        import json

        last = None
        for _ in range(3600):
            current = app_state()
            snapshot = {
                "at": datetime.now(UTC).isoformat(),
                "bus": current.bus.snapshot(),
                "investigations": len(current.runtime.list_investigations()),
                "ledger": current.ledger.counts(),
                "activity": current.activity[-5:],
            }
            payload = json.dumps(snapshot, default=str)
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
            await asyncio.sleep(1.0)

    return StreamingResponse(generate(), media_type="text/event-stream")


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _verdict_summary(current: AppState, verdict_id: str | None) -> dict[str, Any] | None:
    if not verdict_id:
        return None
    verdict = current.ledger.get_verdict(verdict_id)
    if verdict is None:
        return None
    return {
        "status": str(verdict.status),
        "effect_size": verdict.effect_size,
        "control_effect_size": verdict.control_effect_size,
        "reproducibility": verdict.reproducibility,
        "intervention_validity": verdict.intervention_validity,
        "reason_codes": verdict.reason_codes,
    }


def _parse_moment(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid timestamp {value!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@app.exception_handler(AriadneError)
async def ariadne_error_handler(request, exc: AriadneError):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=400,
        content={"error": type(exc).__name__, "detail": str(exc), "retryable": exc.retryable},
    )
