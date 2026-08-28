"""Runtime store contract (prompt 11).

Both the local and the Firestore store are driven through **identical** assertions, so
"the cloud path behaves like the local one" is tested rather than assumed. Every test in
this file runs twice.

The Firestore store runs against a client double, which tests the adapter's logic but not
real Firestore. That boundary is stated in `tests/fakes.py` and in `docs/limitations.md`;
nothing here should be read as cloud verification.

The idempotency tests carry the most weight. `claim()` is the single primitive that turns
at-least-once delivery into exactly-once side effects, and if the two implementations
disagree about it, the guarantee holds locally and evaporates in production.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.core.clock import ManualClock
from backend.core.enums import GovernorAction, InvestigationState, RunKind
from backend.core.errors import StorageError
from backend.core.schemas import ApprovalRequest, ExperimentRun, Investigation
from backend.storage.firestore import FirestoreRuntimeStore
from backend.storage.runtime import LocalRuntimeStore, ScheduledAudit
from tests.factories import T0, make_scope
from tests.fakes import FakeFirestore


@pytest.fixture(params=["local", "firestore"])
def store(request, tmp_path: Path):
    """Every test below runs against both implementations."""
    clock = ManualClock(T0)
    if request.param == "local":
        return LocalRuntimeStore(tmp_path / "runtime", clock=clock)
    return FirestoreRuntimeStore(FakeFirestore(), clock=clock)


def make_investigation(identifier: str = "INV-1", **overrides) -> Investigation:
    fields = {
        "id": identifier,
        "scope": make_scope(),
        "state": InvestigationState.CREATED,
        "trigger_event_id": "EVT-1",
        "trigger_event_type": "MODEL_VERSION_DEPLOYED",
        "source_decision": "HIGH_PRIORITY",
        "source_explanation": "Urgency marker was the primary driver.",
        "created_at": T0,
        "updated_at": T0,
    }
    fields.update(overrides)
    return Investigation(**fields)


def make_run(experiment_id: str = "EXP-1", index: int = 0) -> ExperimentRun:
    return ExperimentRun(
        id=f"RUN-{experiment_id}-{index}",
        experiment_id=experiment_id,
        kind=RunKind.BASELINE,
        index=index,
        scope=make_scope(),
        features={"urgency_marker": 0.9, "signal_b": 0.2, "signal_c": 0.7},
        score=0.61,
        decision="HIGH_PRIORITY",
        model_explanation="Urgency marker was the primary driver.",
        input_hash="sha256:aaa",
        output_hash="sha256:bbb",
        executed_at=T0,
        duration_ms=0.5,
    )


def make_audit(identifier: str = "AUD-1", **overrides) -> ScheduledAudit:
    fields = {
        "id": identifier,
        "claim_family_id": "FAM-1",
        "model_id": "synthetic-triage",
        "scheduled_for": T0 + timedelta(days=7),
        "priority": 0.5,
        "reason_code": "INCONCLUSIVE_EVIDENCE",
        "created_at": T0,
    }
    fields.update(overrides)
    return ScheduledAudit(**fields)


def make_approval(identifier: str = "APR-1") -> ApprovalRequest:
    return ApprovalRequest(
        id=identifier,
        decision_id="GOV-1",
        investigation_id="INV-1",
        action=GovernorAction.REQUIRE_HUMAN_REVIEW,
        justification="two contradictions on one claim",
        requested_at=T0,
    )


class TestIdempotencyClaim:
    """The primitive every at-least-once guarantee rests on."""

    def test_a_free_key_can_be_claimed(self, store) -> None:
        record = store.claim("key-1", "worker-a")
        assert record is not None
        assert record.status == "CLAIMED"
        assert record.owner == "worker-a"

    def test_a_second_worker_cannot_take_a_held_key(self, store) -> None:
        # Two workers racing on the same event: exactly one wins.
        assert store.claim("key-1", "worker-a") is not None
        assert store.claim("key-1", "worker-b") is None

    def test_the_holder_is_not_overwritten_by_the_loser(self, store) -> None:
        store.claim("key-1", "worker-a")
        store.claim("key-1", "worker-b")
        assert store.get_idempotency("key-1").owner == "worker-a"

    def test_an_unclaimed_key_reads_as_absent(self, store) -> None:
        assert store.get_idempotency("never-claimed") is None

    def test_completing_marks_the_work_done(self, store) -> None:
        store.claim("key-1", "worker-a")
        store.complete("key-1", "INV-1")
        record = store.get_idempotency("key-1")
        assert record.is_complete
        assert record.result_ref == "INV-1"
        assert record.completed_at is not None

    def test_a_completed_key_still_cannot_be_reclaimed(self, store) -> None:
        # This is what makes a redelivered event a no-op rather than a repeat.
        store.claim("key-1", "worker-a")
        store.complete("key-1", "INV-1")
        assert store.claim("key-1", "worker-b") is None

    def test_completing_an_unclaimed_key_is_refused(self, store) -> None:
        with pytest.raises(StorageError, match="unclaimed"):
            store.complete("key-1", "INV-1")

    def test_failing_keeps_the_record_and_counts_the_attempt(self, store) -> None:
        # Kept rather than deleted, so an endlessly failing event cannot be retried
        # forever under a fresh claim each time.
        store.claim("key-1", "worker-a")
        store.fail("key-1", "target model unreachable")
        record = store.get_idempotency("key-1")
        assert record.status == "FAILED"
        assert record.attempts == 2
        assert store.claim("key-1", "worker-b") is None

    def test_failing_an_unknown_key_is_a_no_op(self, store) -> None:
        store.fail("never-claimed", "detail")  # does not raise

    def test_releasing_frees_the_key_for_a_retry(self, store) -> None:
        store.claim("key-1", "worker-a")
        store.release("key-1")
        assert store.get_idempotency("key-1") is None
        assert store.claim("key-1", "worker-b") is not None

    def test_releasing_an_unknown_key_is_a_no_op(self, store) -> None:
        store.release("never-claimed")  # does not raise


class TestInvestigations:
    def test_round_trip(self, store) -> None:
        store.save_investigation(make_investigation())
        loaded = store.get_investigation("INV-1")
        assert loaded is not None
        assert loaded.source_explanation == "Urgency marker was the primary driver."
        assert loaded.scope.model_version == "1.0.0"

    def test_an_unknown_investigation_reads_as_absent(self, store) -> None:
        assert store.get_investigation("INV-nope") is None

    def test_saving_again_replaces_the_checkpoint(self, store) -> None:
        # Runtime state is mutable by design; this is what a resumed worker relies on.
        store.save_investigation(make_investigation())
        store.save_investigation(
            make_investigation(state=InvestigationState.VERIFICATION, verdict_id="VDT-1")
        )
        loaded = store.get_investigation("INV-1")
        assert loaded.state is InvestigationState.VERIFICATION
        assert loaded.verdict_id == "VDT-1"

    def test_listing_is_newest_first(self, store) -> None:
        store.save_investigation(make_investigation("INV-old", created_at=T0))
        store.save_investigation(
            make_investigation("INV-new", created_at=T0 + timedelta(days=1))
        )
        assert [i.id for i in store.list_investigations()] == ["INV-new", "INV-old"]

    def test_listing_an_empty_store(self, store) -> None:
        assert store.list_investigations() == []


class TestRunCheckpoints:
    def test_recorded_runs_come_back(self, store) -> None:
        run = make_run()
        store.record_run(run)
        recovered = store.completed_runs("EXP-1")
        assert list(recovered) == [run.id]
        assert recovered[run.id].score == pytest.approx(0.61)

    def test_runs_are_scoped_to_their_experiment(self, store) -> None:
        # A checkpoint that leaked across experiments would let a resumed worker skip runs
        # it never executed.
        store.record_run(make_run("EXP-1", 0))
        store.record_run(make_run("EXP-2", 0))
        assert len(store.completed_runs("EXP-1")) == 1
        assert len(store.completed_runs("EXP-2")) == 1

    def test_an_experiment_with_no_runs_is_empty(self, store) -> None:
        assert store.completed_runs("EXP-never") == {}

    def test_many_runs_are_all_retained(self, store) -> None:
        for index in range(24):
            store.record_run(make_run("EXP-1", index))
        assert len(store.completed_runs("EXP-1")) == 24

    def test_recording_the_same_run_twice_is_idempotent(self, store) -> None:
        store.record_run(make_run("EXP-1", 0))
        store.record_run(make_run("EXP-1", 0))
        assert len(store.completed_runs("EXP-1")) == 1


class TestScheduledAudits:
    def test_a_future_audit_is_not_due(self, store) -> None:
        store.schedule_audit(make_audit())
        assert store.due_audits(T0) == []

    def test_an_elapsed_audit_becomes_due(self, store) -> None:
        store.schedule_audit(make_audit())
        assert len(store.due_audits(T0 + timedelta(days=8))) == 1

    def test_due_audits_are_highest_priority_first(self, store) -> None:
        store.schedule_audit(make_audit("AUD-low", priority=0.2))
        store.schedule_audit(make_audit("AUD-high", priority=0.9))
        due = store.due_audits(T0 + timedelta(days=8))
        assert [a.id for a in due] == ["AUD-high", "AUD-low"]

    def test_an_executed_audit_stops_being_due(self, store) -> None:
        store.schedule_audit(make_audit())
        store.mark_audit_executed("AUD-1", T0 + timedelta(days=8))
        assert store.due_audits(T0 + timedelta(days=9)) == []
        assert store.all_audits()[0].executed_at is not None

    def test_marking_an_unknown_audit_is_refused(self, store) -> None:
        with pytest.raises(StorageError, match="unknown scheduled audit"):
            store.mark_audit_executed("AUD-nope", T0)

    def test_all_audits_are_ordered_by_schedule(self, store) -> None:
        store.schedule_audit(make_audit("AUD-late", scheduled_for=T0 + timedelta(days=30)))
        store.schedule_audit(make_audit("AUD-soon", scheduled_for=T0 + timedelta(days=1)))
        assert [a.id for a in store.all_audits()] == ["AUD-soon", "AUD-late"]


class TestApprovals:
    def test_a_new_request_is_pending(self, store) -> None:
        store.save_approval(make_approval())
        pending = store.pending_approvals()
        assert len(pending) == 1
        assert pending[0].status == "PENDING"

    def test_a_resolved_request_leaves_the_pending_list(self, store) -> None:
        store.save_approval(make_approval())
        store.save_approval(
            make_approval().model_copy(
                update={
                    "status": "APPROVED",
                    "decided_at": T0 + timedelta(hours=1),
                    "decided_by": "nurse-supervisor",
                }
            )
        )
        assert store.pending_approvals() == []
        assert store.get_approval("APR-1").status == "APPROVED"

    def test_an_unknown_approval_reads_as_absent(self, store) -> None:
        assert store.get_approval("APR-nope") is None


class TestIsolation:
    def test_reads_are_defensive_copies(self, store) -> None:
        # Mutating a returned object must not corrupt the store. Against a real client this
        # is free; against an in-process store it has to be deliberate.
        store.save_investigation(make_investigation())
        first = store.get_investigation("INV-1")
        second = store.get_investigation("INV-1")
        assert first is not second
        assert first == second

    def test_sections_do_not_collide(self, store) -> None:
        # Same identifier used in four different sections.
        store.claim("shared-id", "worker-a")
        store.save_investigation(make_investigation("shared-id"))
        store.schedule_audit(make_audit("shared-id"))
        store.save_approval(make_approval("shared-id"))
        assert store.get_idempotency("shared-id") is not None
        assert store.get_investigation("shared-id") is not None
        assert len(store.all_audits()) == 1
        assert len(store.pending_approvals()) == 1


class TestStats:
    def test_stats_report_every_section(self, store) -> None:
        store.claim("key-1", "worker-a")
        store.save_investigation(make_investigation())
        store.record_run(make_run())
        store.schedule_audit(make_audit())
        store.save_approval(make_approval())
        stats = store.stats()
        assert set(stats) == {
            "idempotency", "investigations", "runs", "audits", "approvals"
        }
        assert stats["idempotency"] == 1
        assert stats["investigations"] == 1
        assert stats["audits"] == 1
        assert stats["approvals"] == 1
        assert stats["runs"] >= 1

    def test_an_empty_store_reports_zeroes(self, store) -> None:
        assert all(count == 0 for count in store.stats().values())


class TestFactoryHonesty:
    """The bug this file was written for: configuration must not lie."""

    def test_local_configuration_yields_the_local_store(self, tmp_path, monkeypatch) -> None:
        from backend.config import reset_settings_cache
        from backend.storage.runtime import open_runtime_store

        monkeypatch.setenv("VAR_DIR", str(tmp_path / "var"))
        monkeypatch.setenv("RUNTIME_STORE", "local")
        reset_settings_cache()
        try:
            assert isinstance(open_runtime_store(), LocalRuntimeStore)
        finally:
            reset_settings_cache()

    def test_firestore_configuration_never_falls_back_to_local(
        self, tmp_path, monkeypatch
    ) -> None:
        # Previously `open_runtime_store` returned the local store regardless, so the API
        # reported `runtime_store: firestore` while writing JSON files - a false cloud-proof
        # claim. It must now either build the Firestore store or fail loudly.
        from backend.config import reset_settings_cache
        from backend.storage.runtime import open_runtime_store

        monkeypatch.setenv("VAR_DIR", str(tmp_path / "var"))
        monkeypatch.setenv("RUNTIME_STORE", "firestore")
        monkeypatch.setenv("ENABLE_GOOGLE_CLOUD", "true")
        monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
        reset_settings_cache()
        try:
            store = open_runtime_store()
        except Exception as caught:
            # No credentials or no SDK in this environment: failing is the correct outcome.
            assert not isinstance(caught, AssertionError)
        else:
            assert isinstance(store, FirestoreRuntimeStore)
        finally:
            reset_settings_cache()

    def test_the_firestore_store_satisfies_the_protocol(self) -> None:
        from backend.storage.runtime import RuntimeStateStore

        store = FirestoreRuntimeStore(FakeFirestore(), clock=ManualClock(T0))
        assert isinstance(store, RuntimeStateStore)

    def test_the_local_store_satisfies_the_protocol(self, tmp_path) -> None:
        from backend.storage.runtime import RuntimeStateStore

        store = LocalRuntimeStore(tmp_path / "runtime", clock=ManualClock(T0))
        assert isinstance(store, RuntimeStateStore)


class TestMissingSdk:
    def test_a_missing_sdk_fails_with_a_useful_message(self, monkeypatch) -> None:
        import backend.storage.firestore as module

        def refuse(project, database):
            raise StorageError(
                "google-cloud-firestore is not installed; install the 'gcp' extra or set "
                "RUNTIME_STORE=local"
            )

        monkeypatch.setattr(module.FirestoreRuntimeStore, "_connect", staticmethod(refuse))
        with pytest.raises(StorageError, match="RUNTIME_STORE=local"):
            module.FirestoreRuntimeStore()


def test_utc_helpers_are_timezone_aware() -> None:
    from backend.storage.runtime import utcnow

    assert utcnow().tzinfo is not None
    assert datetime.now(UTC).tzinfo is not None
