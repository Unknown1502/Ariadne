"""The evidence ledger.

Cloud SQL in deployment, SQLite locally, one code path either way. The ledger holds the
authoritative scientific record: claims, plans, runs, evidence, verdicts, lineage, debt
snapshots, and governance decisions.

**Append-only is enforced here, not merely intended.** The ledger exposes no update and no
delete for evidentiary rows. Writing an ID that already exists is allowed only when the
content is byte-identical (which is what makes a retried event safe); writing different
content under an existing ID raises AppendOnlyViolation. That single rule is what lets
Ariadne claim its history is trustworthy - a claim that would be worthless if any code path
could quietly rewrite last month's verdict.

Each row stores the canonical JSON document alongside indexed columns. The document is the
record; the columns exist so queries do not have to parse JSON. Storing the whole document
means a row can always be re-validated against the schema version that wrote it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    select,
)
from sqlalchemy import Column as C
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.core.errors import AppendOnlyViolation, StorageError
from backend.core.hashing import canonical_json, sha256_hex
from backend.core.schemas import (
    AriadneModel,
    Claim,
    DebtSnapshot,
    Evidence,
    ExperimentPlan,
    ExperimentRun,
    GovernorDecision,
    LineageEntry,
    Verdict,
)

metadata = MetaData()

T = TypeVar("T", bound=AriadneModel)


def _document_columns() -> list[C]:
    """Columns every ledger table carries."""
    return [
        C("id", String(64), primary_key=True),
        C("document", Text, nullable=False),
        C("row_hash", String(80), nullable=False),
        C("appended_at", DateTime(timezone=True), nullable=False),
    ]


claims_table = Table(
    "claims",
    metadata,
    *_document_columns(),
    C("claim_family_id", String(64), index=True, nullable=False),
    C("investigation_id", String(64), index=True, nullable=False),
    C("model_id", String(128), index=True, nullable=False),
    C("model_version", String(32), index=True, nullable=False),
    C("distribution_version", String(64), index=True, nullable=False),
    C("subject", String(128), nullable=False),
    C("quarantined", Boolean, nullable=False, default=False),
)

experiments_table = Table(
    "experiments",
    metadata,
    *_document_columns(),
    C("claim_id", String(64), index=True, nullable=False),
    C("investigation_id", String(64), index=True, nullable=False),
    C("model_version", String(32), index=True, nullable=False),
    C("distribution_version", String(64), index=True, nullable=False),
    C("seed", Integer, nullable=False),
    C("repetitions", Integer, nullable=False),
)

runs_table = Table(
    "experiment_runs",
    metadata,
    *_document_columns(),
    C("experiment_id", String(64), index=True, nullable=False),
    C("kind", String(16), index=True, nullable=False),
    C("run_index", Integer, nullable=False),
    C("score", Float, nullable=False),
    C("input_hash", String(80), nullable=False),
    C("output_hash", String(80), nullable=False),
)

evidence_table = Table(
    "evidence",
    metadata,
    *_document_columns(),
    C("experiment_id", String(64), index=True, nullable=False),
    C("claim_id", String(64), index=True, nullable=False),
    C("claim_family_id", String(64), index=True, nullable=False),
    C("model_version", String(32), index=True, nullable=False),
    C("distribution_version", String(64), index=True, nullable=False),
    C("effect_size", Float, nullable=False),
    C("validity_score", Float, nullable=False),
    C("evidence_hash", String(80), nullable=False),
)

verdicts_table = Table(
    "verdicts",
    metadata,
    *_document_columns(),
    C("claim_id", String(64), index=True, nullable=False),
    C("claim_family_id", String(64), index=True, nullable=False),
    C("status", String(16), index=True, nullable=False),
    C("model_version", String(32), index=True, nullable=False),
    C("distribution_version", String(64), index=True, nullable=False),
    C("verifier_version", String(32), nullable=False),
    C("created_at", DateTime(timezone=True), index=True, nullable=False),
)

lineage_table = Table(
    "lineage_entries",
    metadata,
    *_document_columns(),
    C("claim_family_id", String(64), index=True, nullable=False),
    C("claim_id", String(64), index=True, nullable=False),
    C("verdict_id", String(64), index=True, nullable=False),
    C("status", String(16), index=True, nullable=False),
    C("model_id", String(128), index=True, nullable=False),
    C("model_version", String(32), index=True, nullable=False),
    C("distribution_version", String(64), index=True, nullable=False),
    C("relation", String(16), nullable=False),
    C("supersedes_entry_id", String(64), nullable=True),
    C("valid_from", DateTime(timezone=True), index=True, nullable=False),
    C("valid_until", DateTime(timezone=True), index=True, nullable=True),
    C("created_at", DateTime(timezone=True), index=True, nullable=False),
    C("entry_hash", String(80), nullable=False),
    C("previous_entry_hash", String(80), nullable=True),
)

debt_table = Table(
    "debt_snapshots",
    metadata,
    *_document_columns(),
    C("model_id", String(128), index=True, nullable=False),
    C("total", Float, nullable=False),
    C("policy_version", String(32), nullable=False),
    C("computed_at", DateTime(timezone=True), index=True, nullable=False),
)

decisions_table = Table(
    "governor_decisions",
    metadata,
    *_document_columns(),
    C("investigation_id", String(64), index=True, nullable=False),
    C("claim_family_id", String(64), index=True, nullable=False),
    C("action", String(32), index=True, nullable=False),
    C("policy_version", String(32), nullable=False),
    C("required_approval", Boolean, nullable=False),
    C("created_at", DateTime(timezone=True), index=True, nullable=False),
)

audit_table = Table(
    "audit_events",
    metadata,
    *_document_columns(),
    C("investigation_id", String(64), index=True, nullable=True),
    C("event_type", String(48), index=True, nullable=False),
    C("agent_id", String(64), index=True, nullable=True),
    C("occurred_at", DateTime(timezone=True), index=True, nullable=False),
)


class EvidenceLedger:
    """Append-only storage for the scientific record."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        connect_args: dict[str, Any] = {}
        if url.startswith("sqlite"):
            # check_same_thread=False so the API and a worker in the same process can share
            # a connection pool; SQLAlchemy still serializes access.
            connect_args["check_same_thread"] = False
        self.engine: Engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)
        self._session_factory = sessionmaker(bind=self.engine, future=True)
        metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- append ------------------------------------------------------------------------

    def _append(
        self,
        table: Table,
        obj: AriadneModel,
        columns: dict[str, Any],
        *,
        appended_at: datetime | None = None,
    ) -> bool:
        """Insert a document, or accept an identical re-insert. Returns True if inserted.

        The idempotent re-insert is deliberate. At-least-once delivery means the same
        verdict can legitimately be written twice; what must never happen is the same ID
        coming back with *different scientific content*.

        "Different content" is judged on the identity hash, which excludes the volatile
        metadata listed in VOLATILE_FIELDS. That exclusion matters: Ariadne's experiments
        are deterministic, so re-running one produces byte-identical measurements that
        differ only in when they were taken and how many milliseconds they took. Those are
        the same observation, and the record keeps the first sighting rather than
        accumulating near-duplicates or raising a spurious tamper alarm.

        Anything beyond that - a changed score, a changed verdict, a changed hash - is a
        genuine conflict and raises.
        """
        document = canonical_json(obj)
        row_hash = _identity_hash(obj)
        identifier = columns["id"]

        with self.session() as session:
            existing = session.execute(
                select(table.c.row_hash).where(table.c.id == identifier)
            ).first()
            if existing is not None:
                if existing[0] == row_hash:
                    return False  # same observation, already recorded
                raise AppendOnlyViolation(
                    f"{table.name}.{identifier} already exists with different content; "
                    f"historical records are never rewritten"
                )
            session.execute(
                table.insert().values(
                    document=document,
                    row_hash=row_hash,
                    # Callers that already know the timestamp pass it. Sniffing the object
                    # only works for records that happen to use one of the field names
                    # _appended_at knows about, which audit records do not.
                    appended_at=appended_at or _appended_at(obj),
                    **columns,
                )
            )
            return True

    def append_claim(self, claim: Claim) -> None:
        self._append(
            claims_table,
            claim,
            {
                "id": claim.id,
                "claim_family_id": claim.claim_family_id,
                "investigation_id": claim.investigation_id,
                "model_id": claim.scope.model_id,
                "model_version": claim.scope.model_version,
                "distribution_version": claim.scope.distribution_version,
                "subject": claim.subject,
                "quarantined": claim.quarantined,
            },
        )

    def append_plan(self, plan: ExperimentPlan) -> None:
        self._append(
            experiments_table,
            plan,
            {
                "id": plan.id,
                "claim_id": plan.claim_id,
                "investigation_id": plan.investigation_id,
                "model_version": plan.scope.model_version,
                "distribution_version": plan.scope.distribution_version,
                "seed": plan.seed,
                "repetitions": plan.repetitions,
            },
        )

    def append_run(self, run: ExperimentRun) -> None:
        self._append(
            runs_table,
            run,
            {
                "id": run.id,
                "experiment_id": run.experiment_id,
                "kind": str(run.kind),
                "run_index": run.index,
                "score": run.score,
                "input_hash": run.input_hash,
                "output_hash": run.output_hash,
            },
        )

    def append_evidence(self, evidence: Evidence) -> None:
        self._append(
            evidence_table,
            evidence,
            {
                "id": evidence.id,
                "experiment_id": evidence.experiment_id,
                "claim_id": evidence.claim_id,
                "claim_family_id": evidence.claim_family_id,
                "model_version": evidence.scope.model_version,
                "distribution_version": evidence.scope.distribution_version,
                "effect_size": evidence.effect_size,
                "validity_score": evidence.validity_score,
                "evidence_hash": evidence.evidence_hash,
            },
        )

    def append_verdict(self, verdict: Verdict) -> None:
        self._append(
            verdicts_table,
            verdict,
            {
                "id": verdict.id,
                "claim_id": verdict.claim_id,
                "claim_family_id": verdict.claim_family_id,
                "status": str(verdict.status),
                "model_version": verdict.scope.model_version,
                "distribution_version": verdict.scope.distribution_version,
                "verifier_version": verdict.verifier_version,
                "created_at": verdict.created_at,
            },
        )

    def append_lineage(self, entry: LineageEntry) -> None:
        self._append(
            lineage_table,
            entry,
            {
                "id": entry.id,
                "claim_family_id": entry.claim_family_id,
                "claim_id": entry.claim_id,
                "verdict_id": entry.verdict_id,
                "status": str(entry.status),
                "model_id": entry.scope.model_id,
                "model_version": entry.scope.model_version,
                "distribution_version": entry.scope.distribution_version,
                "relation": str(entry.relation),
                "supersedes_entry_id": entry.supersedes_entry_id,
                "valid_from": entry.valid_from,
                "valid_until": entry.valid_until,
                "created_at": entry.created_at,
                "entry_hash": entry.entry_hash,
                "previous_entry_hash": entry.previous_entry_hash,
            },
        )

    def append_debt(self, snapshot: DebtSnapshot) -> None:
        self._append(
            debt_table,
            snapshot,
            {
                "id": snapshot.id,
                "model_id": snapshot.model_id,
                "total": snapshot.total,
                "policy_version": snapshot.policy_version,
                "computed_at": snapshot.computed_at,
            },
        )

    def append_decision(self, decision: GovernorDecision) -> None:
        self._append(
            decisions_table,
            decision,
            {
                "id": decision.id,
                "investigation_id": decision.investigation_id,
                "claim_family_id": decision.claim_family_id,
                "action": str(decision.action),
                "policy_version": decision.policy_version,
                "required_approval": decision.required_approval,
                "created_at": decision.created_at,
            },
        )

    def append_audit(
        self,
        record: AriadneModel,
        *,
        identifier: str,
        event_type: str,
        occurred_at: datetime,
        investigation_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        self._append(
            audit_table,
            record,
            {
                "id": identifier,
                "investigation_id": investigation_id,
                "event_type": event_type,
                "agent_id": agent_id,
                "occurred_at": occurred_at,
            },
            appended_at=occurred_at,
        )

    # -- read --------------------------------------------------------------------------

    def _load(self, model: type[T], table: Table, identifier: str) -> T | None:
        with self.session() as session:
            row = session.execute(
                select(table.c.document).where(table.c.id == identifier)
            ).first()
        return model.model_validate(json.loads(row[0])) if row else None

    def _load_many(self, model: type[T], table: Table, statement) -> list[T]:
        with self.session() as session:
            rows = session.execute(statement).all()
        return [model.model_validate(json.loads(row[0])) for row in rows]

    def get_claim(self, claim_id: str) -> Claim | None:
        return self._load(Claim, claims_table, claim_id)

    def get_plan(self, plan_id: str) -> ExperimentPlan | None:
        return self._load(ExperimentPlan, experiments_table, plan_id)

    def get_evidence(self, evidence_id: str) -> Evidence | None:
        return self._load(Evidence, evidence_table, evidence_id)

    def get_verdict(self, verdict_id: str) -> Verdict | None:
        return self._load(Verdict, verdicts_table, verdict_id)

    def get_lineage_entry(self, entry_id: str) -> LineageEntry | None:
        return self._load(LineageEntry, lineage_table, entry_id)

    def get_debt_snapshot(self, snapshot_id: str) -> DebtSnapshot | None:
        return self._load(DebtSnapshot, debt_table, snapshot_id)

    def get_decision(self, decision_id: str) -> GovernorDecision | None:
        return self._load(GovernorDecision, decisions_table, decision_id)

    def runs_for_experiment(self, experiment_id: str) -> list[ExperimentRun]:
        return self._load_many(
            ExperimentRun,
            runs_table,
            select(runs_table.c.document)
            .where(runs_table.c.experiment_id == experiment_id)
            .order_by(runs_table.c.kind, runs_table.c.run_index),
        )

    def lineage_for_family(self, claim_family_id: str) -> list[LineageEntry]:
        """Complete history for a claim family, oldest first. Nothing is filtered out."""
        return self._load_many(
            LineageEntry,
            lineage_table,
            select(lineage_table.c.document)
            .where(lineage_table.c.claim_family_id == claim_family_id)
            .order_by(lineage_table.c.created_at, lineage_table.c.id),
        )

    def lineage_for_model(self, model_id: str) -> list[LineageEntry]:
        return self._load_many(
            LineageEntry,
            lineage_table,
            select(lineage_table.c.document)
            .where(lineage_table.c.model_id == model_id)
            .order_by(lineage_table.c.created_at, lineage_table.c.id),
        )

    def evidence_for_family(self, claim_family_id: str) -> list[Evidence]:
        return self._load_many(
            Evidence,
            evidence_table,
            select(evidence_table.c.document).where(
                evidence_table.c.claim_family_id == claim_family_id
            ),
        )

    def verdicts_for_family(self, claim_family_id: str) -> list[Verdict]:
        return self._load_many(
            Verdict,
            verdicts_table,
            select(verdicts_table.c.document)
            .where(verdicts_table.c.claim_family_id == claim_family_id)
            .order_by(verdicts_table.c.created_at),
        )

    def claim_families_for_model(self, model_id: str) -> list[str]:
        with self.session() as session:
            rows = session.execute(
                select(claims_table.c.claim_family_id)
                .where(claims_table.c.model_id == model_id)
                .distinct()
            ).all()
        return [row[0] for row in rows]

    def debt_history(self, model_id: str, limit: int = 50) -> list[DebtSnapshot]:
        """Debt snapshots, newest first. The console's trend line reads this."""
        return self._load_many(
            DebtSnapshot,
            debt_table,
            select(debt_table.c.document)
            .where(debt_table.c.model_id == model_id)
            .order_by(debt_table.c.computed_at.desc(), debt_table.c.id.desc())
            .limit(limit),
        )

    def latest_debt(self, model_id: str) -> DebtSnapshot | None:
        history = self.debt_history(model_id, limit=1)
        return history[0] if history else None

    def decisions_for_investigation(self, investigation_id: str) -> list[GovernorDecision]:
        return self._load_many(
            GovernorDecision,
            decisions_table,
            select(decisions_table.c.document)
            .where(decisions_table.c.investigation_id == investigation_id)
            .order_by(decisions_table.c.created_at),
        )

    # -- integrity ---------------------------------------------------------------------

    def verify_integrity(self, table_name: str = "lineage_entries") -> list[str]:
        """Recompute every row hash and report the IDs that no longer match.

        A non-empty result means a row was altered outside the ledger's own API. Exposed as
        a method so integrity is something the system can *demonstrate* on demand rather
        than something the README asserts.
        """
        table = metadata.tables.get(table_name)
        if table is None:
            raise StorageError(f"unknown ledger table {table_name!r}")
        broken: list[str] = []
        with self.session() as session:
            rows = session.execute(select(table.c.id, table.c.document, table.c.row_hash)).all()
        for identifier, document, stored_hash in rows:
            if _identity_hash_from_document(json.loads(document)) != stored_hash:
                broken.append(identifier)
        return broken

    def counts(self) -> dict[str, int]:
        """Row counts per table. Used by the console's cloud-proof panel."""
        from sqlalchemy import func

        result: dict[str, int] = {}
        with self.session() as session:
            for name, table in metadata.tables.items():
                result[name] = int(
                    session.execute(select(func.count()).select_from(table)).scalar() or 0
                )
        return result

    def dispose(self) -> None:
        self.engine.dispose()


VOLATILE_FIELDS: frozenset[str] = frozenset(
    {"created_at", "computed_at", "executed_at", "duration_ms"}
)
"""Top-level fields excluded from identity comparison.

These record *when* an observation was made and how long it took, not *what* was observed.
Ariadne's experiments are deterministic, so two executions of the same plan differ in
exactly these fields and in nothing else. Excluding them is what makes a redelivered event
a no-op instead of a false tamper alarm.

Deliberately a short, explicit list rather than a heuristic: adding a field here weakens the
integrity guarantee, so it should be an obvious decision in a diff.
"""


def _identity_hash_from_document(document: dict[str, Any]) -> str:
    """Hash a stored document's scientific content, ignoring volatile metadata."""
    return sha256_hex({k: v for k, v in document.items() if k not in VOLATILE_FIELDS})


def _identity_hash(obj: AriadneModel) -> str:
    """Hash of an object's scientific content, ignoring volatile metadata.

    Routed through the canonical JSON form rather than hashing the model directly, so that
    a hash computed here and one recomputed from a stored row are computed over identical
    bytes. Hashing the live object would produce a different digest than re-reading it
    (datetimes serialize differently), and every integrity check would fail.
    """
    return _identity_hash_from_document(json.loads(canonical_json(obj)))


def _appended_at(obj: AriadneModel) -> datetime:
    """When the object says it was created, falling back to any timestamp it carries."""
    for field in ("created_at", "computed_at", "executed_at", "valid_from", "requested_at"):
        value = getattr(obj, field, None)
        if isinstance(value, datetime):
            return value
    raise StorageError(f"{type(obj).__name__} carries no timestamp to record")


def open_ledger(url: str | None = None) -> EvidenceLedger:
    """Open the ledger described by configuration."""
    from backend.config import get_settings

    return EvidenceLedger(url or get_settings().resolved_database_url)


def in_memory_ledger() -> EvidenceLedger:
    """A throwaway ledger. Tests use this; nothing in the runtime should."""
    return EvidenceLedger("sqlite://")


def all_table_names() -> Sequence[str]:
    return tuple(metadata.tables)
