"""Runtime state: checkpoints, idempotency, scheduled audits, and approvals.

Firestore in deployment; a durable on-disk store locally. This is deliberately separate
from the evidence ledger, because the two have opposite lifecycles: runtime state is
mutable, short-lived, and exists to make a crashed worker recoverable, while evidence is
immutable and exists forever. Mixing them is how "we resumed the worker" turns into "we
rewrote the verdict".

The local implementation is a directory of JSON documents. That is a deliberate choice over
an embedded database: during the demo you can open the checkpoint file and read it, which
makes crash recovery something a judge can watch rather than take on trust.

Two operations need real atomicity and get it from the filesystem:

  - **Claiming an idempotency key** uses O_CREAT|O_EXCL, which is atomic on both POSIX and
    Windows. Two workers racing on the same event: exactly one wins.
  - **Updating a document** writes a temp file and os.replace()s it, which is atomic. A
    crash mid-write leaves the previous version intact rather than a truncated file.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from backend.core.clock import Clock, SystemClock
from backend.core.errors import StorageError
from backend.core.schemas import ApprovalRequest, ExperimentRun, Investigation


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """The durable answer to 'have we already done this work?'"""

    key: str
    status: str  # CLAIMED | COMPLETED | FAILED
    owner: str
    claimed_at: datetime
    completed_at: datetime | None = None
    result_ref: str | None = None
    attempts: int = 1

    @property
    def is_complete(self) -> bool:
        return self.status == "COMPLETED"

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "owner": self.owner,
            "claimed_at": self.claimed_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "result_ref": self.result_ref,
            "attempts": self.attempts,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> IdempotencyRecord:
        return IdempotencyRecord(
            key=data["key"],
            status=data["status"],
            owner=data["owner"],
            claimed_at=datetime.fromisoformat(data["claimed_at"]),
            completed_at=(
                datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None
            ),
            result_ref=data.get("result_ref"),
            attempts=int(data.get("attempts", 1)),
        )


@dataclass(frozen=True, slots=True)
class ScheduledAudit:
    """A future re-audit the Governor asked for."""

    id: str
    claim_family_id: str
    model_id: str
    scheduled_for: datetime
    priority: float
    reason_code: str
    created_at: datetime
    executed_at: datetime | None = None

    @property
    def is_pending(self) -> bool:
        return self.executed_at is None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "claim_family_id": self.claim_family_id,
            "model_id": self.model_id,
            "scheduled_for": self.scheduled_for.isoformat(),
            "priority": self.priority,
            "reason_code": self.reason_code,
            "created_at": self.created_at.isoformat(),
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> ScheduledAudit:
        return ScheduledAudit(
            id=data["id"],
            claim_family_id=data["claim_family_id"],
            model_id=data["model_id"],
            scheduled_for=datetime.fromisoformat(data["scheduled_for"]),
            priority=float(data["priority"]),
            reason_code=data["reason_code"],
            created_at=datetime.fromisoformat(data["created_at"]),
            executed_at=(
                datetime.fromisoformat(data["executed_at"]) if data.get("executed_at") else None
            ),
        )


@runtime_checkable
class RuntimeStateStore(Protocol):
    """Everything a worker needs to survive being killed.

    Runtime-checkable so the contract suite can assert both implementations satisfy it,
    which is what stops the two drifting apart."""

    def claim(self, key: str, owner: str) -> IdempotencyRecord | None: ...
    def get_idempotency(self, key: str) -> IdempotencyRecord | None: ...
    def complete(self, key: str, result_ref: str) -> None: ...
    def fail(self, key: str, detail: str) -> None: ...
    def release(self, key: str) -> None: ...

    def save_investigation(self, investigation: Investigation) -> None: ...
    def get_investigation(self, investigation_id: str) -> Investigation | None: ...
    def list_investigations(self) -> list[Investigation]: ...

    def record_run(self, run: ExperimentRun) -> None: ...
    def completed_runs(self, experiment_id: str) -> dict[str, ExperimentRun]: ...

    def schedule_audit(self, audit: ScheduledAudit) -> None: ...
    def due_audits(self, now: datetime) -> list[ScheduledAudit]: ...
    def mark_audit_executed(self, audit_id: str, executed_at: datetime) -> None: ...

    def save_approval(self, request: ApprovalRequest) -> None: ...
    def get_approval(self, approval_id: str) -> ApprovalRequest | None: ...
    def pending_approvals(self) -> list[ApprovalRequest]: ...


class LocalRuntimeStore:
    """Durable, inspectable runtime state backed by a directory of JSON documents."""

    def __init__(self, root: Path, clock: Clock | None = None) -> None:
        self.root = Path(root)
        self._clock = clock or SystemClock()
        for section in ("idempotency", "investigations", "runs", "audits", "approvals"):
            (self.root / section).mkdir(parents=True, exist_ok=True)

    # -- idempotency -------------------------------------------------------------------

    def claim(self, key: str, owner: str) -> IdempotencyRecord | None:
        """Try to claim a unit of work.

        Returns the new record on success, or None when someone else already holds it. The
        caller must treat None as "do not perform the side effect" - this is the whole
        at-least-once safety story, and it lives in one atomic file creation.
        """
        path = self._path("idempotency", key)
        record = IdempotencyRecord(
            key=key, status="CLAIMED", owner=owner, claimed_at=self._clock.now()
        )
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return None
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(record.to_json(), stream, indent=2)
        return record

    def get_idempotency(self, key: str) -> IdempotencyRecord | None:
        data = self._read("idempotency", key)
        return IdempotencyRecord.from_json(data) if data else None

    def complete(self, key: str, result_ref: str) -> None:
        record = self.get_idempotency(key)
        if record is None:
            raise StorageError(f"cannot complete unclaimed idempotency key {key!r}")
        self._write(
            "idempotency",
            key,
            IdempotencyRecord(
                key=record.key,
                status="COMPLETED",
                owner=record.owner,
                claimed_at=record.claimed_at,
                completed_at=self._clock.now(),
                result_ref=result_ref,
                attempts=record.attempts,
            ).to_json(),
        )

    def fail(self, key: str, detail: str) -> None:
        """Mark a claim as failed but keep the record.

        Kept rather than deleted so the retry count survives, which is what stops an
        endlessly failing event from being retried forever under a fresh claim each time.
        """
        record = self.get_idempotency(key)
        if record is None:
            return
        self._write(
            "idempotency",
            key,
            IdempotencyRecord(
                key=record.key,
                status="FAILED",
                owner=record.owner,
                claimed_at=record.claimed_at,
                completed_at=self._clock.now(),
                result_ref=detail[:500],
                attempts=record.attempts + 1,
            ).to_json(),
        )

    def release(self, key: str) -> None:
        """Drop a claim so the work can be retried from scratch.

        Only safe when no durable side effect was produced under the claim.
        """
        path = self._path("idempotency", key)
        if path.exists():
            path.unlink()

    # -- investigations ----------------------------------------------------------------

    def save_investigation(self, investigation: Investigation) -> None:
        self._write(
            "investigations", investigation.id, investigation.model_dump(mode="json", by_alias=True)
        )

    def get_investigation(self, investigation_id: str) -> Investigation | None:
        data = self._read("investigations", investigation_id)
        return Investigation.model_validate(data) if data else None

    def list_investigations(self) -> list[Investigation]:
        found = [
            Investigation.model_validate(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted((self.root / "investigations").glob("*.json"))
        ]
        return sorted(found, key=lambda i: i.created_at, reverse=True)

    # -- experiment checkpoints --------------------------------------------------------

    def record_run(self, run: ExperimentRun) -> None:
        self._write(
            "runs", f"{run.experiment_id}__{run.id}", run.model_dump(mode="json", by_alias=True)
        )

    def completed_runs(self, experiment_id: str) -> dict[str, ExperimentRun]:
        runs: dict[str, ExperimentRun] = {}
        for path in (self.root / "runs").glob(f"{_safe(experiment_id)}__*.json"):
            run = ExperimentRun.model_validate(json.loads(path.read_text(encoding="utf-8")))
            runs[run.id] = run
        return runs

    # -- scheduled audits --------------------------------------------------------------

    def schedule_audit(self, audit: ScheduledAudit) -> None:
        self._write("audits", audit.id, audit.to_json())

    def due_audits(self, now: datetime) -> list[ScheduledAudit]:
        """Pending audits whose time has come, highest priority first."""
        due = [
            audit
            for audit in self._all_audits()
            if audit.is_pending and audit.scheduled_for <= now
        ]
        return sorted(due, key=lambda a: (-a.priority, a.scheduled_for))

    def all_audits(self) -> list[ScheduledAudit]:
        return sorted(self._all_audits(), key=lambda a: a.scheduled_for)

    def mark_audit_executed(self, audit_id: str, executed_at: datetime) -> None:
        data = self._read("audits", audit_id)
        if data is None:
            raise StorageError(f"unknown scheduled audit {audit_id!r}")
        audit = ScheduledAudit.from_json(data)
        self._write(
            "audits",
            audit_id,
            ScheduledAudit(
                id=audit.id,
                claim_family_id=audit.claim_family_id,
                model_id=audit.model_id,
                scheduled_for=audit.scheduled_for,
                priority=audit.priority,
                reason_code=audit.reason_code,
                created_at=audit.created_at,
                executed_at=executed_at,
            ).to_json(),
        )

    def _all_audits(self) -> list[ScheduledAudit]:
        return [
            ScheduledAudit.from_json(json.loads(path.read_text(encoding="utf-8")))
            for path in (self.root / "audits").glob("*.json")
        ]

    # -- approvals ---------------------------------------------------------------------

    def save_approval(self, request: ApprovalRequest) -> None:
        self._write("approvals", request.id, request.model_dump(mode="json", by_alias=True))

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        data = self._read("approvals", approval_id)
        return ApprovalRequest.model_validate(data) if data else None

    def pending_approvals(self) -> list[ApprovalRequest]:
        requests = [
            ApprovalRequest.model_validate(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted((self.root / "approvals").glob("*.json"))
        ]
        return [request for request in requests if request.status == "PENDING"]

    # -- helpers -----------------------------------------------------------------------

    def _path(self, section: str, key: str) -> Path:
        return self.root / section / f"{_safe(key)}.json"

    def _read(self, section: str, key: str) -> dict[str, Any] | None:
        path = self._path(section, key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, section: str, key: str, payload: dict[str, Any]) -> None:
        """Atomic document write: a crash mid-write leaves the old version intact."""
        path = self._path(section, key)
        handle, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
            os.replace(temp_name, path)
        except Exception:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def stats(self) -> dict[str, int]:
        """Document counts per section, for the console's runtime-state panel."""
        return {
            section: len(list((self.root / section).glob("*.json")))
            for section in ("idempotency", "investigations", "runs", "audits", "approvals")
        }


def _safe(key: str) -> str:
    """Make an identifier safe to use as a filename without losing uniqueness."""
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in key)


def open_runtime_store(clock: Clock | None = None) -> RuntimeStateStore:
    """Build the configured runtime store.

    Dispatches on RUNTIME_STORE. Returning the local store when Firestore was asked for
    would be worse than failing: the API reports its configuration to the console, so a
    silent fallback would have Ariadne displaying a cloud-proof claim that is not true.
    """
    from backend.config import get_settings

    settings = get_settings()
    if settings.runtime_store == "firestore":
        from backend.storage.firestore import FirestoreRuntimeStore

        return FirestoreRuntimeStore(
            clock=clock,
            database=settings.firestore_database,
            project=settings.gcp_project_id or None,
        )
    return LocalRuntimeStore(settings.runtime_dir, clock=clock)


def default_audit_delay(priority: float) -> timedelta:
    """How far out to schedule a re-audit, given priority.

    Higher priority means sooner. Deterministic rather than adaptive, so the schedule is
    predictable and testable.
    """
    if priority >= 0.9:
        return timedelta(hours=1)
    if priority >= 0.7:
        return timedelta(days=1)
    if priority >= 0.4:
        return timedelta(days=7)
    return timedelta(days=30)


def utcnow() -> datetime:
    return datetime.now(UTC)
