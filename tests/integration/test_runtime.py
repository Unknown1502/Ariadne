"""Asynchronous runtime (prompt 09).

The demo's central claim is that a model-version event starts a full audit with nobody
clicking anything. These tests hold that claim to account, and they hold the reliability
properties that make it safe to say: duplicate events, crashed workers, and retries must
not produce duplicate science.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.core.clock import ManualClock
from backend.core.enums import EventType, InvestigationState, VerdictStatus
from backend.events.bus import LocalEventBus
from backend.lineage.service import LineageService
from backend.runtime.orchestrator import build_pipeline
from backend.runtime.worker import (
    AriadneWorker,
    emit_distribution_changed,
    emit_explanation_received,
    emit_model_version_deployed,
)
from backend.storage.runtime import LocalRuntimeStore
from backend.storage.sql import in_memory_ledger

T0 = datetime(2026, 1, 1, tzinfo=UTC)
MODEL_ID = "synthetic-triage"
BASELINE = "baseline_2024.1"


class Harness:
    """A complete Ariadne runtime in one process, with nothing mocked."""

    def __init__(self, tmp_path: Path) -> None:
        self.ledger = in_memory_ledger()
        self.clock = ManualClock(T0)
        self.runtime = LocalRuntimeStore(tmp_path / "runtime", clock=self.clock)
        self.lineage = LineageService(self.ledger, clock=self.clock)
        self.pipeline = build_pipeline(
            ledger=self.ledger, runtime=self.runtime, clock=self.clock
        )
        self.bus = LocalEventBus(max_attempts=3, base_delay=0.001)
        self.worker = AriadneWorker(
            pipeline=self.pipeline, runtime=self.runtime, lineage=self.lineage,
            bus=self.bus, clock=self.clock, worker_id="worker-a",
        )
        self.bus.subscribe(self.worker.handle)

    async def deploy(self, version: str, distribution: str = BASELINE, *, days: int = 30):
        self.clock.advance(days=days)
        await self.bus.publish(
            emit_model_version_deployed(
                model_id=MODEL_ID, model_version=version,
                distribution_version=distribution, occurred_at=self.clock.now(),
            )
        )
        await self.bus.drain()
        return self.latest()

    def latest(self):
        investigations = self.runtime.list_investigations()
        if not investigations:
            return None
        return max(investigations, key=lambda i: i.updated_at)

    def family(self) -> str:
        families = self.lineage.families_for_model(MODEL_ID)
        return families[0] if families else ""

    def verdict_of(self, investigation):
        return self.ledger.get_verdict(investigation.verdict_id or "")

    def decision_of(self, investigation):
        return self.ledger.get_decision(investigation.decision_id or "")


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    built = Harness(tmp_path)
    yield built
    built.ledger.dispose()


class TestAutonomousAudit:
    async def test_a_model_event_runs_a_full_audit_with_no_user_action(
        self, harness: Harness
    ) -> None:
        # The demo's headline: publish an event, and an investigation completes on its own.
        investigation = await harness.deploy("1.0.0")
        assert investigation is not None
        assert investigation.state in (InvestigationState.COMPLETE, InvestigationState.REVIEW)
        assert harness.verdict_of(investigation).status is VerdictStatus.CONTRADICTED
        assert harness.decision_of(investigation) is not None

    async def test_the_four_versions_produce_the_demo_lineage(
        self, harness: Harness
    ) -> None:
        expected = {
            "1.0.0": VerdictStatus.CONTRADICTED,
            "2.0.0": VerdictStatus.SUPPORTED,
            "3.0.0": VerdictStatus.INCONCLUSIVE,
            "4.0.0": VerdictStatus.CONTRADICTED,
        }
        for version in expected:
            investigation = await harness.deploy(version)
            assert harness.verdict_of(investigation).status is expected[version]

        view = harness.lineage.view(harness.family())
        assert view.statuses_by_version == expected

    async def test_the_investigation_walked_the_whole_state_machine(
        self, harness: Harness
    ) -> None:
        investigation = await harness.deploy("2.0.0")
        # Every artifact the pipeline is supposed to produce exists and is linked.
        assert investigation.claim_id
        assert investigation.experiment_id
        assert investigation.evidence_id
        assert investigation.verdict_id
        assert investigation.lineage_entry_id
        assert investigation.debt_snapshot_id
        assert investigation.decision_id

    async def test_prior_contradictions_raise_the_next_audits_priority(
        self, harness: Harness
    ) -> None:
        # The memory that makes the audit targeted rather than exhaustive.
        await harness.deploy("1.0.0")
        priority_after_contradiction = harness.lineage.audit_priority(harness.family())
        assert priority_after_contradiction > 0.5

    async def test_a_human_triggered_explanation_uses_the_same_pipeline(
        self, harness: Harness
    ) -> None:
        harness.clock.advance(days=1)
        await harness.bus.publish(
            emit_explanation_received(
                model_id=MODEL_ID, model_version="1.0.0", distribution_version=BASELINE,
                decision="HIGH_PRIORITY",
                explanation="Urgency marker was the primary driver.",
                occurred_at=harness.clock.now(), case_id="case-1",
            )
        )
        await harness.bus.drain()
        investigation = harness.latest()
        assert investigation.trigger_event_type == "EXPLANATION_RECEIVED"
        assert harness.verdict_of(investigation).status is VerdictStatus.CONTRADICTED


class TestIdempotency:
    async def test_a_redelivered_event_does_no_work_twice(self, harness: Harness) -> None:
        event = emit_model_version_deployed(
            model_id=MODEL_ID, model_version="1.0.0",
            distribution_version=BASELINE, occurred_at=harness.clock.now(),
        )
        await harness.bus.publish(event)
        await harness.bus.drain()
        before = harness.ledger.counts()

        for _ in range(5):
            await harness.bus.publish_duplicate(event)
        await harness.bus.drain()

        assert harness.ledger.counts() == before
        assert harness.worker.stats.duplicates_skipped == 5

    async def test_two_producers_of_the_same_fact_collapse_to_one_audit(
        self, harness: Harness
    ) -> None:
        # Different event_ids, same work. Only the idempotency key should matter.
        first = emit_model_version_deployed(
            model_id=MODEL_ID, model_version="1.0.0",
            distribution_version=BASELINE, occurred_at=harness.clock.now(),
        )
        second = emit_model_version_deployed(
            model_id=MODEL_ID, model_version="1.0.0",
            distribution_version=BASELINE, occurred_at=harness.clock.now(),
        )
        assert first.event_id != second.event_id
        assert first.idempotency_key == second.idempotency_key

        await harness.bus.publish(first)
        await harness.bus.drain()
        await harness.bus.publish(second)
        await harness.bus.drain()
        assert len(harness.runtime.list_investigations()) == 1

    async def test_the_same_version_on_a_different_distribution_is_different_work(
        self, harness: Harness
    ) -> None:
        # Regression: the idempotency key once ignored the distribution, so a genuine
        # re-audit under shifted data was silently skipped as a duplicate.
        baseline_event = emit_model_version_deployed(
            model_id=MODEL_ID, model_version="2.0.0",
            distribution_version=BASELINE, occurred_at=harness.clock.now(),
        )
        shifted_event = emit_model_version_deployed(
            model_id=MODEL_ID, model_version="2.0.0",
            distribution_version="shifted_2025.2", occurred_at=harness.clock.now(),
        )
        assert baseline_event.idempotency_key != shifted_event.idempotency_key

        await harness.bus.publish(baseline_event)
        await harness.bus.drain()
        await harness.bus.publish(shifted_event)
        await harness.bus.drain()
        assert len(harness.runtime.list_investigations()) == 2

    async def test_out_of_order_events_are_each_handled_once(
        self, harness: Harness
    ) -> None:
        await harness.deploy("3.0.0")
        await harness.deploy("1.0.0")
        await harness.deploy("2.0.0")
        assert len(harness.runtime.list_investigations()) == 3
        # Lineage records what actually happened, in arrival order.
        assert len(harness.lineage.history(harness.family())) == 3

    async def test_an_idempotency_record_survives_completion(
        self, harness: Harness
    ) -> None:
        event = emit_model_version_deployed(
            model_id=MODEL_ID, model_version="1.0.0",
            distribution_version=BASELINE, occurred_at=harness.clock.now(),
        )
        await harness.bus.publish(event)
        await harness.bus.drain()
        record = harness.runtime.get_idempotency(event.idempotency_key)
        assert record is not None and record.is_complete


class TestCrashRecovery:
    async def test_a_second_worker_resumes_an_abandoned_investigation(
        self, harness: Harness
    ) -> None:
        # Worker A dies mid-pipeline; its checkpoint is all Worker B has.
        await harness.deploy("1.0.0")
        investigation = harness.latest()

        stalled = investigation.model_copy(
            update={"state": InvestigationState.EXPERIMENT_RUNNING}
        )
        harness.runtime.save_investigation(stalled)

        result = harness.pipeline.resume(investigation.id)
        assert result.investigation.state in (
            InvestigationState.COMPLETE, InvestigationState.REVIEW
        )
        assert result.resumed_from is InvestigationState.EXPERIMENT_RUNNING

    async def test_resumption_does_not_duplicate_evidence(self, harness: Harness) -> None:
        await harness.deploy("1.0.0")
        investigation = harness.latest()
        before = harness.ledger.counts()

        harness.runtime.save_investigation(
            investigation.model_copy(update={"state": InvestigationState.VERIFICATION})
        )
        harness.pipeline.resume(investigation.id)

        after = harness.ledger.counts()
        assert after["evidence"] == before["evidence"]
        assert after["experiment_runs"] == before["experiment_runs"]

    async def test_experiment_runs_are_checkpointed_as_they_happen(
        self, harness: Harness
    ) -> None:
        await harness.deploy("1.0.0")
        investigation = harness.latest()
        checkpointed = harness.runtime.completed_runs(investigation.experiment_id)
        assert len(checkpointed) == 24 * 3  # baseline + intervention + control

    async def test_resuming_an_unknown_investigation_fails_clearly(
        self, harness: Harness
    ) -> None:
        from backend.core.errors import ValidationError

        with pytest.raises(ValidationError, match="no checkpoint exists"):
            harness.pipeline.resume("INV-never-existed")

    async def test_a_completed_investigation_is_not_rerun(self, harness: Harness) -> None:
        await harness.deploy("2.0.0")
        investigation = harness.latest()
        before = harness.ledger.counts()
        result = harness.pipeline.resume(investigation.id)
        assert harness.ledger.counts() == before
        assert result.investigation.state is investigation.state


class TestDistributionShift:
    async def test_a_distribution_event_expires_evidence_without_rewriting_it(
        self, harness: Harness
    ) -> None:
        await harness.deploy("2.0.0")
        family = harness.family()
        history_before = len(harness.lineage.history(family))

        harness.clock.advance(days=1)
        await harness.bus.publish(
            emit_distribution_changed(
                model_id=MODEL_ID, distribution_version="shifted_2025.2",
                previous_distribution_version=BASELINE,
                occurred_at=harness.clock.now(), drift_score=0.7,
                affected_features=["urgency_marker"],
            )
        )
        await harness.bus.drain()

        assert harness.lineage.current_evidence(family) is None
        assert len(harness.lineage.history(family)) > history_before

    async def test_the_reaudit_after_a_shift_is_inconclusive_not_contradicted(
        self, harness: Harness
    ) -> None:
        # The intervention can no longer move the input enough to test anything. Reporting
        # CONTRADICTED here would be a fabricated refutation.
        await harness.deploy("2.0.0")
        harness.clock.advance(days=1)
        await harness.bus.publish(
            emit_distribution_changed(
                model_id=MODEL_ID, distribution_version="shifted_2025.2",
                previous_distribution_version=BASELINE, occurred_at=harness.clock.now(),
            )
        )
        await harness.bus.drain()

        investigation = await harness.deploy("2.0.0", "shifted_2025.2", days=1)
        verdict = harness.verdict_of(investigation)
        assert verdict.status is VerdictStatus.INCONCLUSIVE
        assert "WEAK_PERTURBATION" in verdict.reason_codes

    async def test_an_expiry_event_is_published(self, harness: Harness) -> None:
        await harness.deploy("2.0.0")
        harness.clock.advance(days=1)
        await harness.bus.publish(
            emit_distribution_changed(
                model_id=MODEL_ID, distribution_version="shifted_2025.2",
                previous_distribution_version=BASELINE, occurred_at=harness.clock.now(),
            )
        )
        await harness.bus.drain()
        published = [str(e.event_type) for e in harness.bus.published_events]
        assert "EVIDENCE_EXPIRED" in published


class TestGovernanceEffects:
    async def test_repeated_contradictions_open_an_approval_request(
        self, harness: Harness
    ) -> None:
        await harness.deploy("1.0.0")
        investigation = await harness.deploy("4.0.0")
        assert investigation.state is InvestigationState.REVIEW
        pending = harness.runtime.pending_approvals()
        assert len(pending) == 1
        assert pending[0].status == "PENDING"

    async def test_a_high_impact_action_waits_rather_than_executing(
        self, harness: Harness
    ) -> None:
        await harness.deploy("1.0.0")
        investigation = await harness.deploy("4.0.0")
        decision = harness.decision_of(investigation)
        assert decision.required_approval is True
        # The investigation is parked in REVIEW, not COMPLETE.
        assert investigation.state is InvestigationState.REVIEW

    async def test_a_reaudit_is_scheduled_for_later(self, harness: Harness) -> None:
        await harness.deploy("3.0.0")  # INCONCLUSIVE -> SCHEDULE_REAUDIT
        audits = harness.runtime.all_audits()
        assert audits
        assert audits[0].scheduled_for > harness.clock.now()

    async def test_a_scheduled_audit_does_nothing_before_it_is_due(
        self, harness: Harness
    ) -> None:
        await harness.deploy("3.0.0")
        assert await harness.worker.run_due_audits() == []

    async def test_a_due_audit_actually_re_runs_an_investigation(
        self, harness: Harness
    ) -> None:
        # Regression: this once marked audits executed without auditing anything, so the
        # "Ariadne schedules its own follow-up" claim was a stub with a confident docstring.
        await harness.deploy("3.0.0")
        harness.clock.advance(days=400)

        published_before = len(harness.bus.published_events)
        executed = await harness.worker.run_due_audits()
        await harness.bus.drain()

        assert executed, "a due audit should have run"
        assert len(harness.bus.published_events) > published_before, (
            "a scheduled re-audit must emit a real event, not just flip a flag"
        )
        emitted = harness.bus.published_events[-1]
        assert emitted.payload.deployed_by.startswith("ariadne-scheduler")

    async def test_an_executed_audit_is_not_run_twice(self, harness: Harness) -> None:
        await harness.deploy("3.0.0")
        harness.clock.advance(days=400)
        assert await harness.worker.run_due_audits()
        await harness.bus.drain()
        assert await harness.worker.run_due_audits() == []

    async def test_debt_is_recorded_on_every_audit(self, harness: Harness) -> None:
        await harness.deploy("1.0.0")
        await harness.deploy("2.0.0")
        history = harness.ledger.debt_history(MODEL_ID)
        assert len(history) == 2
        assert all(0.0 <= s.total <= 100.0 for s in history)


class TestBusReliability:
    async def test_a_failing_handler_is_retried_then_dead_lettered(
        self, tmp_path: Path
    ) -> None:
        bus = LocalEventBus(max_attempts=3, base_delay=0.0)
        attempts: list[int] = []

        async def always_fails(event):
            attempts.append(event.attempt)
            raise RuntimeError("downstream unavailable")

        bus.subscribe(always_fails)
        await bus.publish(
            emit_model_version_deployed(
                model_id=MODEL_ID, model_version="1.0.0",
                distribution_version=BASELINE, occurred_at=T0,
            )
        )
        await bus.drain()

        assert attempts == [1, 2, 3]
        assert len(bus.dead_letters) == 1
        assert bus.dead_letters[0].error_code == "RuntimeError"
        assert bus.stats.dead_lettered == 1

    async def test_a_non_retryable_failure_dead_letters_immediately(
        self, tmp_path: Path
    ) -> None:
        from backend.core.errors import ValidationError

        bus = LocalEventBus(max_attempts=5, base_delay=0.0)

        async def rejects(event):
            raise ValidationError("this event is structurally wrong")

        bus.subscribe(rejects)
        await bus.publish(
            emit_model_version_deployed(
                model_id=MODEL_ID, model_version="1.0.0",
                distribution_version=BASELINE, occurred_at=T0,
            )
        )
        await bus.drain()
        assert len(bus.dead_letters) == 1
        assert bus.dead_letters[0].attempts == 1

    async def test_a_retry_keeps_the_same_identity(self, tmp_path: Path) -> None:
        bus = LocalEventBus(max_attempts=3, base_delay=0.0)
        seen = []

        async def flaky(event):
            seen.append((event.event_id, event.idempotency_key))
            raise RuntimeError("transient")

        bus.subscribe(flaky)
        await bus.publish(
            emit_model_version_deployed(
                model_id=MODEL_ID, model_version="1.0.0",
                distribution_version=BASELINE, occurred_at=T0,
            )
        )
        await bus.drain()
        assert len({identity for identity in seen}) == 1

    async def test_random_duplication_does_not_corrupt_state(
        self, tmp_path: Path
    ) -> None:
        # Simulates Pub/Sub redelivering under load.
        harness = Harness(tmp_path)
        harness.bus = LocalEventBus(max_attempts=3, base_delay=0.0, duplicate_rate=1.0)
        harness.worker = AriadneWorker(
            pipeline=harness.pipeline, runtime=harness.runtime, lineage=harness.lineage,
            bus=harness.bus, clock=harness.clock,
        )
        harness.bus.subscribe(harness.worker.handle)

        for version in ("1.0.0", "2.0.0"):
            harness.clock.advance(days=30)
            await harness.bus.publish(
                emit_model_version_deployed(
                    model_id=MODEL_ID, model_version=version,
                    distribution_version=BASELINE, occurred_at=harness.clock.now(),
                )
            )
            await harness.bus.drain()

        assert len(harness.runtime.list_investigations()) == 2
        assert harness.worker.stats.duplicates_skipped >= 2
        assert len(harness.lineage.history(harness.family())) == 2
        harness.ledger.dispose()


class TestEventContract:
    def test_the_wake_up_event_carries_everything_needed(self) -> None:
        event = emit_model_version_deployed(
            model_id=MODEL_ID, model_version="2.0.0",
            distribution_version=BASELINE, occurred_at=T0, deployed_by="ci",
        )
        assert event.event_type is EventType.MODEL_VERSION_DEPLOYED
        assert event.payload.model_version == "2.0.0"
        assert event.idempotency_key
        assert event.producer == "model-registry"

    def test_events_round_trip_through_transport(self) -> None:
        from backend.events.schemas import parse_event

        event = emit_distribution_changed(
            model_id=MODEL_ID, distribution_version="shifted_2025.2",
            previous_distribution_version=BASELINE, occurred_at=T0, drift_score=0.4,
        )
        restored = parse_event(event.model_dump(mode="json"))
        assert restored.payload == event.payload
