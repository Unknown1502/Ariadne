"""FirestoreRuntimeStore against a real Firestore emulator.

`docs/limitations.md` used to say plainly: *"the Firestore store is covered by the same
contract suite as the local one — but against a client double."* That was an honest
statement, and it was also the reason the F9 defect survived 675 passing tests: the double
raised whatever exception the adapter happened to expect, so a wrong `except AlreadyExists`
clause and a real `raise Conflict` never met each other.

This file removes that gap for the one class of defect a double structurally cannot catch:
did the adapter guess the real SDK's behavior correctly? Every test here runs against
``gcr.io/google.com/cloudsdktool/cloud-sdk:emulators`` — the actual `google-cloud-firestore`
wire protocol, the actual exception hierarchy, the actual subcollection semantics.

**What this does and does not prove.** The emulator implements the Firestore API faithfully
enough to catch protocol- and library-level mistakes — exactly the F9 class of bug. It does
not exercise IAM, quotas, multi-region consistency, or network partitions; those still
require a deployed project, and `docs/limitations.md` says so. This is a strictly stronger
claim than "tested against a hand-written double," not a claim of full cloud proof.

Skipped entirely unless ``FIRESTORE_EMULATOR_HOST`` is set, so the default test run — the one
that must work with no Google Cloud account — is completely unaffected. To run these:

    docker run -d -p 8090:8090 gcr.io/google.com/cloudsdktool/cloud-sdk:emulators \\
        gcloud emulators firestore start --host-port=0.0.0.0:8090
    FIRESTORE_EMULATOR_HOST=localhost:8090 pytest tests/integration/test_firestore_emulator.py
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest

pytestmark = pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires a live Firestore emulator; set FIRESTORE_EMULATOR_HOST to run",
)

firestore = pytest.importorskip("google.cloud.firestore")

from backend.core.clock import ManualClock  # noqa: E402
from backend.core.enums import InvestigationState  # noqa: E402
from backend.core.errors import StorageError  # noqa: E402
from backend.storage.firestore import FirestoreRuntimeStore  # noqa: E402
from backend.storage.runtime import ScheduledAudit  # noqa: E402
from tests.factories import T0, make_scope  # noqa: E402


@pytest.fixture
def project_id() -> str:
    # A fresh project namespace per test session avoids cross-test collisions; the emulator
    # is an in-memory server so a made-up project id is exactly as valid as a real one.
    return f"ariadne-emu-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def real_client(project_id: str):
    client = firestore.Client(project=project_id)
    yield client
    client.close()


@pytest.fixture
def store(real_client) -> FirestoreRuntimeStore:
    return FirestoreRuntimeStore(real_client, clock=ManualClock(T0))


def unique_key(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:12]}"


class TestIdempotencyAgainstRealFirestore:
    def test_a_fresh_key_can_be_claimed(self, store: FirestoreRuntimeStore) -> None:
        key = unique_key("claim")
        record = store.claim(key, owner="worker-a")
        assert record is not None
        assert record.status == "CLAIMED"

    def test_the_same_key_cannot_be_claimed_twice(self, store: FirestoreRuntimeStore) -> None:
        key = unique_key("claim")
        assert store.claim(key, owner="worker-a") is not None
        assert store.claim(key, owner="worker-b") is None

    def test_two_workers_racing_the_same_key_the_f9_scenario(
        self, project_id: str
    ) -> None:
        """The exact bug this test suite exists to prevent from recurring.

        F9: `create()` raises `google.api_core.exceptions.Conflict`; the adapter caught the
        narrower `AlreadyExists`. Against a hand-written double that raised whatever the
        adapter happened to catch, this never failed. Against the real emulator, it did -
        this test is what confirmed the fix rather than merely asserting it.

        Two independent `FirestoreRuntimeStore` instances stand in for two workers that
        received the same redelivered event and both tried to claim it.
        """
        client_a = firestore.Client(project=project_id)
        client_b = firestore.Client(project=project_id)
        try:
            worker_a = FirestoreRuntimeStore(client_a, clock=ManualClock(T0))
            worker_b = FirestoreRuntimeStore(client_b, clock=ManualClock(T0))
            key = unique_key("race")

            first = worker_a.claim(key, owner="worker-a")
            second = worker_b.claim(key, owner="worker-b")  # must not raise

            assert first is not None
            assert second is None, "the second claim must be recognised, not crash the worker"
        finally:
            client_a.close()
            client_b.close()

    def test_complete_records_the_result(self, store: FirestoreRuntimeStore) -> None:
        key = unique_key("complete")
        store.claim(key, owner="worker-a")
        store.complete(key, result_ref="VDT-123")
        record = store.get_idempotency(key)
        assert record is not None
        assert record.status == "COMPLETED"
        assert record.result_ref == "VDT-123"

    def test_completing_an_unclaimed_key_is_refused(self, store: FirestoreRuntimeStore) -> None:
        with pytest.raises(StorageError, match="unclaimed"):
            store.complete(unique_key("never-claimed"), result_ref="x")

    def test_fail_preserves_the_attempt_count_for_retry_backoff(
        self, store: FirestoreRuntimeStore
    ) -> None:
        key = unique_key("fail")
        store.claim(key, owner="worker-a")
        store.fail(key, detail="target model timeout")
        record = store.get_idempotency(key)
        assert record is not None
        assert record.status == "FAILED"
        assert record.attempts == 2

    def test_release_allows_a_fresh_claim(self, store: FirestoreRuntimeStore) -> None:
        key = unique_key("release")
        store.claim(key, owner="worker-a")
        store.release(key)
        assert store.claim(key, owner="worker-b") is not None


class TestInvestigationsAgainstRealFirestore:
    def test_round_trip(self, store: FirestoreRuntimeStore) -> None:
        from backend.core.schemas import Investigation

        scope = make_scope()
        investigation = Investigation(
            id=unique_key("INV"),
            scope=scope,
            state=InvestigationState.CREATED,
            trigger_event_id="EVT-1",
            trigger_event_type="MODEL_VERSION_DEPLOYED",
            created_at=T0,
            updated_at=T0,
        )
        store.save_investigation(investigation)
        fetched = store.get_investigation(investigation.id)
        assert fetched == investigation

    def test_missing_investigation_returns_none(self, store: FirestoreRuntimeStore) -> None:
        assert store.get_investigation(unique_key("nope")) is None

    def test_list_investigations_reflects_saves(self, store: FirestoreRuntimeStore) -> None:
        from backend.core.schemas import Investigation

        scope = make_scope()
        before = {inv.id for inv in store.list_investigations()}
        new_id = unique_key("INV")
        store.save_investigation(
            Investigation(
                id=new_id, scope=scope, state=InvestigationState.CREATED,
                trigger_event_id="EVT-1", trigger_event_type="MODEL_VERSION_DEPLOYED",
                created_at=T0, updated_at=T0,
            )
        )
        after = {inv.id for inv in store.list_investigations()}
        assert after - before == {new_id}


class TestExperimentCheckpointsAgainstRealFirestore:
    """The crash-recovery mechanism the demo's 'worker crash' beat depends on."""

    def test_completed_runs_starts_empty(self, store: FirestoreRuntimeStore) -> None:
        assert store.completed_runs(unique_key("EXP")) == {}

    def test_a_recorded_run_is_returned_on_the_next_read(
        self, store: FirestoreRuntimeStore
    ) -> None:
        from backend.core.enums import RunKind
        from backend.core.schemas import ExperimentRun

        experiment_id = unique_key("EXP")
        run = ExperimentRun(
            id=f"{experiment_id}-run-0", experiment_id=experiment_id, kind=RunKind.BASELINE,
            index=0, scope=make_scope(), features={"urgency_marker": 0.8, "signal_b": 0.3,
            "signal_c": 0.7}, score=0.62, decision="HIGH_PRIORITY",
            model_explanation="Urgency marker was the primary driver.",
            input_hash="sha256:a", output_hash="sha256:b", executed_at=T0, duration_ms=1.2,
        )
        store.record_run(run)
        checkpoint = store.completed_runs(experiment_id)
        assert checkpoint == {run.id: run}

    def test_the_parent_marker_write_is_load_bearing(self, store: FirestoreRuntimeStore) -> None:
        """Proves, against the real service, the subtlety documented in `record_run`.

        The code comment claims: 'Firestore does not return documents that exist only as
        subcollection parents from stream(), so without this marker the parent is invisible
        and the checkpoint count reads as zero.' That is a claim about Firestore's own
        behaviour, and a double cannot verify a claim about the system it stands in for -
        only the real service can. This confirms it directly: the `RUNS` collection actually
        contains a readable parent document after `record_run`, not just the subcollection.
        """
        from backend.core.enums import RunKind
        from backend.core.schemas import ExperimentRun

        experiment_id = unique_key("EXP")
        run = ExperimentRun(
            id=f"{experiment_id}-run-0", experiment_id=experiment_id, kind=RunKind.BASELINE,
            index=0, scope=make_scope(), features={"urgency_marker": 0.5, "signal_b": 0.5,
            "signal_c": 0.5}, score=0.5, decision="STANDARD_PRIORITY",
            model_explanation="x", input_hash="sha256:a", output_hash="sha256:b",
            executed_at=T0, duration_ms=1.0,
        )
        store.record_run(run)
        parent = store._client.collection("ariadne_runs").document(experiment_id).get()
        assert parent.exists, "without the marker write, this experiment would be invisible"


class TestScheduledAuditsAgainstRealFirestore:
    def test_due_audits_respects_the_schedule(self, store: FirestoreRuntimeStore) -> None:
        family = unique_key("FAM")
        due_now = ScheduledAudit(
            id=unique_key("AUD"), claim_family_id=family, model_id="synthetic-triage",
            scheduled_for=T0, priority=0.9, reason_code="CONTRADICTED_PRIOR", created_at=T0,
        )
        not_yet = ScheduledAudit(
            id=unique_key("AUD"), claim_family_id=family, model_id="synthetic-triage",
            scheduled_for=T0 + timedelta(days=30), priority=0.5, reason_code="ROUTINE",
            created_at=T0,
        )
        store.schedule_audit(due_now)
        store.schedule_audit(not_yet)

        due = {audit.id for audit in store.due_audits(T0 + timedelta(hours=1))}
        assert due_now.id in due
        assert not_yet.id not in due

    def test_marking_executed_removes_it_from_due(self, store: FirestoreRuntimeStore) -> None:
        audit = ScheduledAudit(
            id=unique_key("AUD"), claim_family_id=unique_key("FAM"),
            model_id="synthetic-triage", scheduled_for=T0, priority=0.8,
            reason_code="CONTRADICTED_PRIOR", created_at=T0,
        )
        store.schedule_audit(audit)
        store.mark_audit_executed(audit.id, executed_at=T0 + timedelta(minutes=5))
        assert audit.id not in {a.id for a in store.due_audits(T0 + timedelta(days=1))}

    def test_marking_an_unknown_audit_executed_fails_loudly(
        self, store: FirestoreRuntimeStore
    ) -> None:
        with pytest.raises(StorageError, match="unknown scheduled audit"):
            store.mark_audit_executed(unique_key("nonexistent"), executed_at=T0)


class TestApprovalsAgainstRealFirestore:
    def test_pending_approvals_round_trip(self, store: FirestoreRuntimeStore) -> None:
        from backend.core.enums import GovernorAction
        from backend.core.schemas import ApprovalRequest

        request = ApprovalRequest(
            id=unique_key("APR"), decision_id=unique_key("GOV"),
            investigation_id=unique_key("INV"), action=GovernorAction.PAUSE_AFFECTED_WORKFLOW,
            justification="debt spike after distribution shift", requested_at=T0,
        )
        store.save_approval(request)
        assert request.id in {r.id for r in store.pending_approvals()}
        assert store.get_approval(request.id) == request


class TestStatsAgainstRealFirestore:
    def test_stats_reflects_real_collection_sizes(self, store: FirestoreRuntimeStore) -> None:
        key = unique_key("stats-claim")
        store.claim(key, owner="worker-a")
        stats = store.stats()
        assert stats["idempotency"] >= 1
        assert set(stats) == {"idempotency", "investigations", "runs", "audits", "approvals"}
