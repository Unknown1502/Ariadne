"""Firestore runtime state.

The cloud counterpart to ``LocalRuntimeStore``. Both satisfy the same ``RuntimeStateStore``
protocol and both are exercised by the same contract suite, so "the cloud path behaves like
the local one" is a tested claim rather than an assumption.

The one operation that must be exactly right is **claiming an idempotency key**, because
every at-least-once guarantee rests on it. Locally that is `O_CREAT|O_EXCL`; here it is
``DocumentReference.create()``, which fails if the document already exists. Both are atomic,
and both give the same answer to two workers racing on the same event: exactly one wins.

Two deliberate shapes:

  - **Runs are stored in a subcollection** under their experiment
    (``runs/{experiment_id}/items/{run_id}``) rather than in a flat collection with a query.
    Listing a checkpoint is then a plain subcollection read - no composite index to create,
    no query API to get wrong, and no risk of a partial index returning a *subset* of the
    completed runs, which would silently re-execute work.
  - **Small collections are streamed and filtered in Python.** Scheduled audits and approval
    requests number in the dozens. A server-side query would buy nothing and would couple
    this module to a query API that has changed shape across SDK versions.

The SDK is imported lazily so the project stays installable and testable without the `gcp`
extra.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from backend.core.clock import Clock, SystemClock
from backend.core.configuration import (
    Connection,
    ExplanationSource,
    FeatureSemantics,
    ReceivedExplanation,
)
from backend.core.errors import StorageError
from backend.core.schemas import ApprovalRequest, ExperimentRun, Investigation
from backend.storage.runtime import IdempotencyRecord, ScheduledAudit

IDEMPOTENCY = "ariadne_idempotency"
INVESTIGATIONS = "ariadne_investigations"
RUNS = "ariadne_runs"
RUN_ITEMS = "items"
AUDITS = "ariadne_audits"
APPROVALS = "ariadne_approvals"
CONNECTIONS = "ariadne_connections"
FEATURES = "ariadne_features"
EXPLANATION_SOURCES = "ariadne_explanation_sources"
EXPLANATIONS = "ariadne_explanations"


class DocumentExists(Exception):
    """A document already exists.

    Mirrors what ``DocumentReference.create()`` raises, so a client double can signal the
    same condition without the project depending on google-api-core.
    """


def _conflict_errors() -> tuple[type[BaseException], ...]:
    """Exception types that mean 'this document was already created'.

    **Catch ``Conflict``, not ``AlreadyExists``.** This looks like a nit and is not.
    Firestore maps the gRPC status explicitly::

        _GRPC_ERROR_MAPPING = {grpc.StatusCode.ALREADY_EXISTS: exceptions.Conflict, ...}

    so ``create()`` raises ``Conflict`` — the *parent* class. ``AlreadyExists`` is a subclass
    of it, and catching a subclass does not catch the parent. An earlier version of this
    function caught only ``AlreadyExists``, which meant that on the Firestore path a second
    worker racing for the same idempotency key got an *unhandled* exception instead of the
    ``None`` that says "someone else has this". Duplicate-event safety — the guarantee this
    method exists to provide — was broken in the cloud while every local test passed.

    ``Conflict`` covers both, so this is strictly the wider and more correct catch.
    """
    errors: list[type[BaseException]] = [DocumentExists]
    try:  # pragma: no cover - only importable with the gcp extra
        from google.api_core import exceptions as google_errors

        errors.append(google_errors.Conflict)
    except ImportError:
        pass
    return tuple(errors)


class FirestoreRuntimeStore:
    """Runtime state backed by Firestore."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        clock: Clock | None = None,
        database: str = "(default)",
        project: str | None = None,
    ) -> None:
        self._client = client if client is not None else self._connect(project, database)
        self._clock = clock or SystemClock()

    @staticmethod
    def _connect(project: str | None, database: str) -> Any:  # pragma: no cover - needs GCP
        try:
            from google.cloud import firestore
        except ImportError as exc:
            raise StorageError(
                "google-cloud-firestore is not installed; install the 'gcp' extra or set "
                "RUNTIME_STORE=local"
            ) from exc
        if database and database != "(default)":
            return firestore.Client(project=project, database=database)
        return firestore.Client(project=project)

    # -- idempotency -------------------------------------------------------------------

    def claim(self, key: str, owner: str) -> IdempotencyRecord | None:
        """Try to claim a unit of work. None means someone else already holds it.

        ``create()`` is the atomic primitive: it fails rather than overwriting. Using
        ``set()`` here would silently stomp another worker's claim and hand the same event
        to two workers, which is the exact failure this method exists to prevent.
        """
        record = IdempotencyRecord(
            key=key, status="CLAIMED", owner=owner, claimed_at=self._clock.now()
        )
        try:
            self._doc(IDEMPOTENCY, key).create(record.to_json())
        except _conflict_errors():
            return None
        return record

    def get_idempotency(self, key: str) -> IdempotencyRecord | None:
        data = self._read(IDEMPOTENCY, key)
        return IdempotencyRecord.from_json(data) if data else None

    def complete(self, key: str, result_ref: str) -> None:
        record = self.get_idempotency(key)
        if record is None:
            raise StorageError(f"cannot complete unclaimed idempotency key {key!r}")
        self._doc(IDEMPOTENCY, key).set(
            IdempotencyRecord(
                key=record.key,
                status="COMPLETED",
                owner=record.owner,
                claimed_at=record.claimed_at,
                completed_at=self._clock.now(),
                result_ref=result_ref,
                attempts=record.attempts,
            ).to_json()
        )

    def fail(self, key: str, detail: str) -> None:
        """Mark a claim failed but keep the record, so the retry count survives."""
        record = self.get_idempotency(key)
        if record is None:
            return
        self._doc(IDEMPOTENCY, key).set(
            IdempotencyRecord(
                key=record.key,
                status="FAILED",
                owner=record.owner,
                claimed_at=record.claimed_at,
                completed_at=self._clock.now(),
                result_ref=detail[:500],
                attempts=record.attempts + 1,
            ).to_json()
        )

    def release(self, key: str) -> None:
        """Drop a claim so the work can be retried. Safe only when nothing durable was written."""
        self._doc(IDEMPOTENCY, key).delete()

    # -- investigations ----------------------------------------------------------------

    def save_investigation(self, investigation: Investigation) -> None:
        self._doc(INVESTIGATIONS, investigation.id).set(
            investigation.model_dump(mode="json", by_alias=True)
        )

    def get_investigation(self, investigation_id: str) -> Investigation | None:
        data = self._read(INVESTIGATIONS, investigation_id)
        return Investigation.model_validate(data) if data else None

    def list_investigations(self) -> list[Investigation]:
        found = [
            Investigation.model_validate(document)
            for document in self._stream(INVESTIGATIONS)
        ]
        return sorted(found, key=lambda i: i.created_at, reverse=True)

    # -- experiment checkpoints --------------------------------------------------------

    def record_run(self, run: ExperimentRun) -> None:
        self._run_items(run.experiment_id).document(run.id).set(
            run.model_dump(mode="json", by_alias=True)
        )
        # Firestore does not return documents that exist only as subcollection parents from
        # stream(), so without this marker the parent is invisible and the checkpoint count
        # reads as zero however many runs are stored. One small write per run is cheaper
        # than a read to decide whether to write it.
        self._client.collection(RUNS).document(run.experiment_id).set(
            {"experiment_id": run.experiment_id, "last_run_at": run.executed_at.isoformat()}
        )

    def completed_runs(self, experiment_id: str) -> dict[str, ExperimentRun]:
        runs: dict[str, ExperimentRun] = {}
        for snapshot in self._run_items(experiment_id).stream():
            run = ExperimentRun.model_validate(snapshot.to_dict())
            runs[run.id] = run
        return runs

    def _run_items(self, experiment_id: str) -> Any:
        return (
            self._client.collection(RUNS).document(experiment_id).collection(RUN_ITEMS)
        )

    # -- scheduled audits --------------------------------------------------------------

    def schedule_audit(self, audit: ScheduledAudit) -> None:
        self._doc(AUDITS, audit.id).set(audit.to_json())

    def due_audits(self, now: datetime) -> list[ScheduledAudit]:
        due = [
            audit for audit in self.all_audits() if audit.is_pending and audit.scheduled_for <= now
        ]
        return sorted(due, key=lambda a: (-a.priority, a.scheduled_for))

    def all_audits(self) -> list[ScheduledAudit]:
        audits = [ScheduledAudit.from_json(document) for document in self._stream(AUDITS)]
        return sorted(audits, key=lambda a: a.scheduled_for)

    def mark_audit_executed(self, audit_id: str, executed_at: datetime) -> None:
        data = self._read(AUDITS, audit_id)
        if data is None:
            raise StorageError(f"unknown scheduled audit {audit_id!r}")
        audit = ScheduledAudit.from_json(data)
        self._doc(AUDITS, audit_id).set(
            ScheduledAudit(
                id=audit.id,
                claim_family_id=audit.claim_family_id,
                model_id=audit.model_id,
                scheduled_for=audit.scheduled_for,
                priority=audit.priority,
                reason_code=audit.reason_code,
                created_at=audit.created_at,
                executed_at=executed_at,
            ).to_json()
        )

    # -- approvals ---------------------------------------------------------------------

    def save_approval(self, request: ApprovalRequest) -> None:
        self._doc(APPROVALS, request.id).set(request.model_dump(mode="json", by_alias=True))

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        data = self._read(APPROVALS, approval_id)
        return ApprovalRequest.model_validate(data) if data else None

    def pending_approvals(self) -> list[ApprovalRequest]:
        requests = [
            ApprovalRequest.model_validate(document) for document in self._stream(APPROVALS)
        ]
        return sorted(
            (request for request in requests if request.status == "PENDING"),
            key=lambda r: r.requested_at,
        )

    # -- helpers -----------------------------------------------------------------------

    # -- operator configuration --------------------------------------------------------
    #
    # Deliberately plain document CRUD. These are mutable configuration records, so none of
    # the idempotency-claim machinery above applies - the atomic-create dance exists to stop
    # two workers doing the same science twice, and editing a neutral value is neither.

    def save_connection(self, connection: Connection) -> None:
        self._doc(CONNECTIONS, connection.id).set(connection.model_dump(mode="json"))

    def get_connection(self, connection_id: str) -> Connection | None:
        data = self._read(CONNECTIONS, connection_id)
        return Connection.model_validate(data) if data else None

    def list_connections(self) -> list[Connection]:
        return sorted(
            (Connection.model_validate(d) for d in self._stream(CONNECTIONS)),
            key=lambda c: c.created_at,
        )

    def delete_connection(self, connection_id: str) -> bool:
        return self._delete_doc(CONNECTIONS, connection_id)

    def save_feature(self, feature: FeatureSemantics) -> None:
        self._doc(FEATURES, feature.id).set(feature.model_dump(mode="json"))

    def get_feature(self, feature_id: str) -> FeatureSemantics | None:
        data = self._read(FEATURES, feature_id)
        return FeatureSemantics.model_validate(data) if data else None

    def list_features(self, model_id: str | None = None) -> list[FeatureSemantics]:
        features = [FeatureSemantics.model_validate(d) for d in self._stream(FEATURES)]
        if model_id is not None:
            features = [f for f in features if f.model_id == model_id]
        return sorted(features, key=lambda f: f.name)

    def delete_feature(self, feature_id: str) -> bool:
        return self._delete_doc(FEATURES, feature_id)

    def save_explanation_source(self, source: ExplanationSource) -> None:
        self._doc(EXPLANATION_SOURCES, source.id).set(source.model_dump(mode="json"))

    def get_explanation_source(self, source_id: str) -> ExplanationSource | None:
        data = self._read(EXPLANATION_SOURCES, source_id)
        return ExplanationSource.model_validate(data) if data else None

    def list_explanation_sources(self) -> list[ExplanationSource]:
        return sorted(
            (ExplanationSource.model_validate(d) for d in self._stream(EXPLANATION_SOURCES)),
            key=lambda s: s.created_at,
        )

    def delete_explanation_source(self, source_id: str) -> bool:
        return self._delete_doc(EXPLANATION_SOURCES, source_id)

    def save_explanation(self, explanation: ReceivedExplanation) -> None:
        self._doc(EXPLANATIONS, explanation.id).set(explanation.model_dump(mode="json"))

    def list_explanations(self, model_id: str | None = None) -> list[ReceivedExplanation]:
        items = [ReceivedExplanation.model_validate(d) for d in self._stream(EXPLANATIONS)]
        if model_id is not None:
            items = [e for e in items if e.model_id == model_id]
        return sorted(items, key=lambda e: e.received_at, reverse=True)

    def _delete_doc(self, collection: str, key: str) -> bool:
        reference = self._doc(collection, key)
        if not reference.get().exists:
            return False
        reference.delete()
        return True

    def _doc(self, collection: str, key: str) -> Any:
        return self._client.collection(collection).document(key)

    def _read(self, collection: str, key: str) -> dict[str, Any] | None:
        snapshot = self._doc(collection, key).get()
        return snapshot.to_dict() if snapshot.exists else None

    def _stream(self, collection: str) -> list[dict[str, Any]]:
        return [snapshot.to_dict() for snapshot in self._client.collection(collection).stream()]

    def stats(self) -> dict[str, int]:
        """Document counts per section, for the console's runtime-state panel.

        ``runs`` counts experiments with checkpoints rather than individual runs: counting
        every run would mean a read per experiment on every dashboard poll, and the number
        the panel needs is "is anything checkpointed", not an exact total.
        """
        return {
            "idempotency": len(self._stream(IDEMPOTENCY)),
            "investigations": len(self._stream(INVESTIGATIONS)),
            "runs": len(list(self._client.collection(RUNS).stream())),
            "audits": len(self._stream(AUDITS)),
            "approvals": len(self._stream(APPROVALS)),
        }
