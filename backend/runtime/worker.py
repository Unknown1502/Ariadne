"""The worker.

This is where "a model version was deployed" becomes an audit that nobody asked for.

The worker owns delivery semantics; the pipeline owns the science. Its whole job is to turn
an at-least-once stream of events into exactly-once side effects, and it does that with one
mechanism: **claim before doing.**

    claim(idempotency_key)  ->  None means someone else already has it: stop.
                            ->  a record means we own it: do the work, then complete it.

The claim is an atomic file creation locally and a Firestore transaction in deployment.
Because it happens *before* any side effect, a redelivered event finds the key taken and
returns without re-running the experiment. Because the claim is released on a retryable
failure, a genuine crash does not wedge the event forever.

The second layer is that the work itself is idempotent anyway: investigation IDs, experiment
IDs, and evidence IDs are content-addressed, so even a duplicate that slipped past the claim
would resolve to the same rows rather than creating new ones. Two independent guarantees,
because this is the property the whole "fortified fleet" claim rests on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from backend.core.clock import Clock, SystemClock
from backend.core.enums import EventType, LineageRelation
from backend.core.errors import AriadneError
from backend.core.ids import random_id
from backend.core.schemas import VersionScope
from backend.events.bus import EventBus
from backend.events.schemas import (
    AriadneEvent,
    DistributionChangedPayload,
    EvidenceExpiredPayload,
    ExplanationReceivedPayload,
    ModelVersionDeployedPayload,
    make_event,
)
from backend.experiment_engine.target_model import STANDING_EXPLANATION
from backend.lineage.service import LineageService
from backend.runtime.orchestrator import InvestigationPipeline, InvestigationRequest
from backend.storage.runtime import RuntimeStateStore

WORKER_ID_PREFIX = "worker"


@dataclass
class WorkerStats:
    events_seen: int = 0
    events_processed: int = 0
    duplicates_skipped: int = 0
    investigations_started: int = 0
    failures: int = 0
    handled_types: dict[str, int] = field(default_factory=dict)


class AriadneWorker:
    """Consumes events and drives investigations."""

    def __init__(
        self,
        *,
        pipeline: InvestigationPipeline,
        runtime: RuntimeStateStore,
        lineage: LineageService,
        bus: EventBus | None = None,
        clock: Clock | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._runtime = runtime
        self._lineage = lineage
        self._bus = bus
        self._clock = clock or SystemClock()
        self.worker_id = worker_id or random_id(WORKER_ID_PREFIX)
        self.stats = WorkerStats()

    # -- entry point -------------------------------------------------------------------

    async def handle(self, event: AriadneEvent) -> None:
        """Process one delivery. Safe to call twice with the same event."""
        self.stats.events_seen += 1
        self.stats.handled_types[str(event.event_type)] = (
            self.stats.handled_types.get(str(event.event_type), 0) + 1
        )

        claim = self._runtime.claim(event.idempotency_key, self.worker_id)
        if claim is None:
            existing = self._runtime.get_idempotency(event.idempotency_key)
            if existing is not None and existing.is_complete:
                # Already done. This is the duplicate-event guarantee, and it is the
                # ordinary path rather than an error.
                self.stats.duplicates_skipped += 1
                if self._bus is not None:
                    self._bus.note_duplicate_suppressed()
                return
            # Claimed but unfinished: another worker holds it, or a previous attempt died.
            # Resuming is safe because every step is checkpointed and content-addressed.
            self.stats.duplicates_skipped += 1
            if self._bus is not None:
                self._bus.note_duplicate_suppressed()
            return

        try:
            result_ref = await self._dispatch(event)
        except AriadneError as exc:
            self.stats.failures += 1
            if getattr(exc, "retryable", False):
                # Release the claim so the retry can pick the work up again. The
                # investigation checkpoint survives, so the retry resumes mid-pipeline.
                self._runtime.release(event.idempotency_key)
            else:
                self._runtime.fail(event.idempotency_key, f"{type(exc).__name__}: {exc}")
            raise
        except Exception:
            self.stats.failures += 1
            self._runtime.release(event.idempotency_key)
            raise

        self._runtime.complete(event.idempotency_key, result_ref or "no-op")
        self.stats.events_processed += 1

    # -- dispatch ----------------------------------------------------------------------

    async def _dispatch(self, event: AriadneEvent) -> str:
        match event.event_type:
            case EventType.MODEL_VERSION_DEPLOYED:
                return self._on_model_version_deployed(event)
            case EventType.DISTRIBUTION_CHANGED:
                return await self._on_distribution_changed(event)
            case EventType.EXPLANATION_RECEIVED:
                return self._on_explanation_received(event)
            case _:
                # Informational events are recorded by the claim itself; there is no work
                # to do, and pretending otherwise would invent activity.
                return f"acknowledged:{event.event_type}"

    def _on_model_version_deployed(self, event: AriadneEvent) -> str:
        """The autonomous path. Nobody asked for this audit.

        A new version arrives, prior lineage says which explanations are most in doubt, and
        the highest-priority claim family is re-tested against the new version.
        """
        payload = event.payload
        assert isinstance(payload, ModelVersionDeployedPayload)

        scope = VersionScope(
            model_id=payload.model_id,
            model_version=payload.model_version,
            distribution_version=payload.distribution_version,
        )

        families = self._lineage.families_affected_by_version(
            payload.model_id, payload.model_version
        )
        priority = (
            max(self._lineage.audit_priority(family) for family in families)
            if families
            else 0.5
        )

        request = InvestigationRequest(
            scope=scope,
            # The explanation under test is the one the model itself ships. Ariadne does
            # not invent a claim to test; it re-tests the standing one.
            explanation=STANDING_EXPLANATION,
            decision="HIGH_PRIORITY",
            trigger_event_id=event.event_id,
            trigger_event_type=str(event.event_type),
            priority=priority,
        )
        result = self._pipeline.run(request)
        if result.resumed_from is None:
            self.stats.investigations_started += 1
        return result.investigation.id

    async def _on_distribution_changed(self, event: AriadneEvent) -> str:
        """Data drifted, so evidence measured under the old distribution stops being current.

        Note what this does *not* do: it does not flip any verdict. The old results remain
        exactly as true as they were, about the distribution they were measured on. They
        simply stop describing today.
        """
        payload = event.payload
        assert isinstance(payload, DistributionChangedPayload)

        superseded = payload.previous_distribution_version
        expired_ids: list[str] = []
        affected = self._lineage.families_affected_by_distribution(
            payload.model_id, payload.distribution_version
        )

        for family in affected:
            entries = self._lineage.expire_evidence(
                family,
                reason=(
                    f"DISTRIBUTION_CHANGED to {payload.distribution_version}"
                    + (f" from {superseded}" if superseded else "")
                ),
                distribution_version=superseded,
                at=self._clock.now(),
            )
            expired_ids.extend(entry.id for entry in entries)

        if expired_ids and self._bus is not None:
            await self._bus.publish(
                make_event(
                    EventType.EVIDENCE_EXPIRED,
                    EvidenceExpiredPayload(
                        claim_family_id=affected[0],
                        entry_ids=expired_ids,
                        reason=f"distribution {payload.distribution_version}",
                    ),
                    aggregate_id=payload.model_id,
                    aggregate_version=payload.distribution_version,
                    occurred_at=self._clock.now(),
                    causation_id=event.event_id,
                ),
            )

        return f"expired:{len(expired_ids)}"

    def _on_explanation_received(self, event: AriadneEvent) -> str:
        """A human-triggered investigation. The same pipeline, a different trigger."""
        payload = event.payload
        assert isinstance(payload, ExplanationReceivedPayload)

        scope = VersionScope(
            model_id=payload.model_id,
            model_version=payload.model_version,
            distribution_version=payload.distribution_version,
        )
        result = self._pipeline.run(
            InvestigationRequest(
                scope=scope,
                explanation=payload.explanation,
                decision=payload.decision,
                trigger_event_id=event.event_id,
                trigger_event_type=str(event.event_type),
                priority=0.5,
            )
        )
        if result.resumed_from is None:
            self.stats.investigations_started += 1
        return result.investigation.id

    # -- scheduled work ----------------------------------------------------------------

    async def run_due_audits(self, now: datetime | None = None) -> list[str]:
        """Execute re-audits the Governor scheduled and whose time has come.

        This is the other half of autonomy: the Governor asked for a future check, and the
        worker performs it later without anyone re-triggering it.

        Each due audit is turned back into a MODEL_VERSION_DEPLOYED event and published, so
        a scheduled re-audit takes exactly the same path as an externally triggered one -
        same idempotency, same checkpoints, same lineage. A separate "internal" code path
        would be a second implementation of the pipeline that nothing else tests.

        The audit is marked executed only after its event is accepted. Marking first would
        make a publish failure look like completed work.
        """
        moment = now or self._clock.now()
        executed: list[str] = []

        for audit in self._runtime.due_audits(moment):
            scope = self._latest_scope_for(audit.claim_family_id)
            if scope is None:
                # Nothing to re-test: the family has no non-expired reading to re-audit
                # against. Mark it done rather than retrying a scheduled audit forever.
                self._runtime.mark_audit_executed(audit.id, moment)
                continue

            event = emit_model_version_deployed(
                model_id=audit.model_id,
                model_version=scope.model_version,
                distribution_version=scope.distribution_version,
                occurred_at=moment,
                deployed_by=f"ariadne-scheduler:{audit.reason_code}",
            )
            if self._bus is not None:
                await self._bus.publish(event)
            else:
                await self.handle(event)

            self._runtime.mark_audit_executed(audit.id, moment)
            executed.append(audit.id)

        return executed

    def _latest_scope_for(self, claim_family_id: str) -> VersionScope | None:
        """The scope a scheduled re-audit should target.

        The most recent reading for the family, expired or not: an expiry is precisely the
        reason to re-test, so filtering expired entries out here would make the audit the
        Governor scheduled impossible to run.
        """
        entries = [
            entry
            for entry in self._lineage.history(claim_family_id)
            if entry.relation is not LineageRelation.EXPIRES
        ]
        return entries[-1].scope if entries else None


def build_worker(
    *,
    pipeline: InvestigationPipeline,
    runtime: RuntimeStateStore,
    lineage: LineageService,
    bus: EventBus | None = None,
    clock: Clock | None = None,
    worker_id: str | None = None,
) -> AriadneWorker:
    return AriadneWorker(
        pipeline=pipeline,
        runtime=runtime,
        lineage=lineage,
        bus=bus,
        clock=clock,
        worker_id=worker_id,
    )


def emit_model_version_deployed(
    *,
    model_id: str,
    model_version: str,
    distribution_version: str,
    occurred_at: datetime,
    deployed_by: str | None = None,
) -> AriadneEvent:
    """Build the event that wakes Ariadne up."""
    return make_event(
        EventType.MODEL_VERSION_DEPLOYED,
        ModelVersionDeployedPayload(
            model_id=model_id,
            model_version=model_version,
            distribution_version=distribution_version,
            deployed_at=occurred_at,
            deployed_by=deployed_by,
        ),
        aggregate_id=model_id,
        # The distribution belongs in the aggregate version, not just the payload. The
        # idempotency key is derived from (event_type, aggregate_id, aggregate_version), so
        # omitting it would make "v2.0.0 on the original data" and "v2.0.0 on the shifted
        # data" the same unit of work - and the second, genuinely different audit would be
        # silently skipped as a duplicate.
        aggregate_version=f"{model_version}@{distribution_version}",
        occurred_at=occurred_at,
        producer="model-registry",
    )


def emit_distribution_changed(
    *,
    model_id: str,
    distribution_version: str,
    previous_distribution_version: str | None,
    occurred_at: datetime,
    drift_score: float = 0.0,
    affected_features: list[str] | None = None,
) -> AriadneEvent:
    return make_event(
        EventType.DISTRIBUTION_CHANGED,
        DistributionChangedPayload(
            model_id=model_id,
            distribution_version=distribution_version,
            previous_distribution_version=previous_distribution_version,
            drift_score=drift_score,
            affected_features=affected_features or [],
            detected_at=occurred_at,
        ),
        aggregate_id=model_id,
        aggregate_version=distribution_version,
        occurred_at=occurred_at,
        producer="drift-monitor",
    )


def emit_explanation_received(
    *,
    model_id: str,
    model_version: str,
    distribution_version: str,
    decision: str,
    explanation: str,
    occurred_at: datetime,
    case_id: str | None = None,
) -> AriadneEvent:
    return make_event(
        EventType.EXPLANATION_RECEIVED,
        ExplanationReceivedPayload(
            model_id=model_id,
            model_version=model_version,
            distribution_version=distribution_version,
            decision=decision,
            explanation=explanation,
            case_id=case_id,
            received_at=occurred_at,
        ),
        aggregate_id=model_id,
        aggregate_version=f"{model_version}:{case_id or 'adhoc'}",
        occurred_at=occurred_at,
        producer="triage-console",
    )
