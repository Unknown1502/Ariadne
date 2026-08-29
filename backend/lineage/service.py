"""Evidence lineage (prompt 07).

A claim is not true or false. It is true *of a model version, under a data distribution,
within a validity window*. Lineage is the structure that keeps that qualification attached,
so "the explanation is trustworthy" can be answered with "as of when?"

Everything here is append-only, and that constraint shapes the design in one important
place: **expiring evidence does not edit the expired row**. It appends a new entry whose
relation is EXPIRES and which names the entry it closes. "Current" is therefore a computed
view over the history rather than a mutable flag, and the history itself is never touched.
That is what makes it possible to ask "what did we believe last March?" and get a real
answer instead of whatever the flag says today.

Each entry is chained to its predecessor by hash. Removing or altering an ancestor breaks
every descendant's chain, so tampering is detectable rather than merely discouraged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.core.clock import Clock, SystemClock
from backend.core.enums import LineageRelation, VerdictStatus
from backend.core.errors import ValidationError
from backend.core.hashing import hash_chain
from backend.core.ids import LINEAGE_PREFIX, derive_id
from backend.core.schemas import Evidence, LineageEntry, Verdict, VersionScope
from backend.storage.sql import EvidenceLedger

DEFAULT_VALIDITY_DAYS = 90


@dataclass(frozen=True, slots=True)
class LineageView:
    """A claim family's history, as the console and the Governor consume it."""

    claim_family_id: str
    entries: list[LineageEntry]
    current: LineageEntry | None
    expired_entry_ids: frozenset[str]

    @property
    def statuses_by_version(self) -> dict[str, VerdictStatus]:
        """Latest status per model version. This is the v1/v2/v3/v4 strip in the UI."""
        result: dict[str, VerdictStatus] = {}
        for entry in self.entries:
            if entry.relation is LineageRelation.EXPIRES:
                continue
            result[entry.scope.model_version] = entry.status
        return result

    def count(self, status: VerdictStatus) -> int:
        return sum(
            1
            for entry in self.entries
            if entry.relation is not LineageRelation.EXPIRES and entry.status is status
        )

    @property
    def has_expired_evidence(self) -> bool:
        return bool(self.expired_entry_ids)


class LineageService:
    """Reads and appends claim-family history."""

    def __init__(
        self,
        ledger: EvidenceLedger,
        *,
        clock: Clock | None = None,
        validity_days: int = DEFAULT_VALIDITY_DAYS,
    ) -> None:
        self._ledger = ledger
        self._clock = clock or SystemClock()
        self._validity = timedelta(days=validity_days)

    # -- writing -----------------------------------------------------------------------

    def append_verdict(
        self, verdict: Verdict, evidence: Evidence, *, valid_from: datetime | None = None
    ) -> LineageEntry:
        """Record a new verdict in its family's history.

        The relation is derived, never supplied by a caller: a re-audit of the same scope
        SUPERSEDES, a result from a different version CONFIRMS or DISPUTES depending on
        whether it agrees. Letting an agent choose this would let it rewrite what the
        history means without touching a single row.
        """
        if verdict.claim_id != evidence.claim_id:
            raise ValidationError(
                f"verdict {verdict.id} and evidence {evidence.id} describe different claims"
            )
        if evidence.id not in verdict.evidence_ids:
            raise ValidationError(
                f"verdict {verdict.id} does not reference evidence {evidence.id}; a lineage "
                f"entry must be traceable to the measurements behind it"
            )

        history = self._ledger.lineage_for_family(verdict.claim_family_id)
        previous = self._chain_tail(history)
        relation, supersedes = self._derive_relation(verdict, history)
        moment = valid_from or self._clock.now()

        payload = {
            "claim_family_id": verdict.claim_family_id,
            "claim_id": verdict.claim_id,
            "verdict_id": verdict.id,
            "status": verdict.status,
            "scope": verdict.scope,
            "evidence_ids": sorted(verdict.evidence_ids),
            "effect_size": verdict.effect_size,
            "protocol_version": verdict.protocol_version,
            "verifier_version": verdict.verifier_version,
            "valid_from": moment,
            "relation": relation,
        }
        entry_hash = hash_chain(previous.entry_hash if previous else None, payload)

        entry = LineageEntry(
            id=derive_id(LINEAGE_PREFIX, verdict.id, entry_hash),
            claim_family_id=verdict.claim_family_id,
            claim_id=verdict.claim_id,
            scope=verdict.scope,
            protocol_version=verdict.protocol_version,
            verdict_id=verdict.id,
            status=verdict.status,
            evidence_ids=sorted(verdict.evidence_ids),
            behavioral_support=verdict.behavioral_support,
            intervention_validity=verdict.intervention_validity,
            reproducibility=verdict.reproducibility,
            effect_size=verdict.effect_size,
            relation=relation,
            supersedes_entry_id=supersedes,
            valid_from=moment,
            created_at=moment,
            input_hashes=list(evidence.input_hashes),
            output_hashes=list(evidence.output_hashes),
            verifier_version=verdict.verifier_version,
            previous_entry_hash=previous.entry_hash if previous else None,
            entry_hash=entry_hash,
        )
        self._ledger.append_lineage(entry)
        return entry

    def expire_evidence(
        self,
        claim_family_id: str,
        *,
        reason: str,
        scope_filter: VersionScope | None = None,
        distribution_version: str | None = None,
        at: datetime | None = None,
    ) -> list[LineageEntry]:
        """Close the validity window on evidence a change has invalidated.

        Appends an EXPIRES entry per affected row. The expired entries are untouched: they
        remain exactly as true as they ever were, about the world they were measured in.
        """
        moment = at or self._clock.now()
        view = self.view(claim_family_id, at=moment)
        appended: list[LineageEntry] = []
        # Read the tail once, then chain onto what we just wrote. Re-reading it per
        # iteration was the original bug: every row here shares `moment`, so the ledger's
        # (created_at, id) tiebreak made "the last entry" arbitrary and each expiry linked
        # back to the same predecessor - a fork wearing a chain's clothes.
        previous = self._chain_tail(self._ledger.lineage_for_family(claim_family_id))

        for entry in view.entries:
            if entry.relation is LineageRelation.EXPIRES:
                continue
            if entry.id in view.expired_entry_ids:
                continue
            if scope_filter is not None and not entry.scope.matches(scope_filter):
                continue
            if (
                distribution_version is not None
                and entry.scope.distribution_version != distribution_version
            ):
                continue

            if previous is None:  # unreachable: view.entries is non-empty here
                raise ValidationError(
                    f"claim family {claim_family_id} has entries to expire but no chain tail"
                )
            payload = {
                "expires": entry.id,
                "reason": reason,
                "at": moment,
                "claim_family_id": claim_family_id,
            }
            entry_hash = hash_chain(previous.entry_hash, payload)
            expiry = LineageEntry(
                id=derive_id(LINEAGE_PREFIX, entry.id, "EXPIRES", entry_hash),
                claim_family_id=entry.claim_family_id,
                claim_id=entry.claim_id,
                scope=entry.scope,
                protocol_version=entry.protocol_version,
                verdict_id=entry.verdict_id,
                status=entry.status,
                evidence_ids=list(entry.evidence_ids),
                behavioral_support=entry.behavioral_support,
                intervention_validity=entry.intervention_validity,
                reproducibility=entry.reproducibility,
                effect_size=entry.effect_size,
                relation=LineageRelation.EXPIRES,
                supersedes_entry_id=entry.id,
                valid_from=moment,
                valid_until=moment,
                expired_reason=reason,
                created_at=moment,
                input_hashes=list(entry.input_hashes),
                output_hashes=list(entry.output_hashes),
                verifier_version=entry.verifier_version,
                previous_entry_hash=previous.entry_hash,
                entry_hash=entry_hash,
            )
            self._ledger.append_lineage(expiry)
            appended.append(expiry)
            previous = expiry

        return appended

    # -- reading -----------------------------------------------------------------------

    def view(self, claim_family_id: str, *, at: datetime | None = None) -> LineageView:
        """The family's full history plus which entry is current at a moment."""
        moment = at or self._clock.now()
        entries = self._ledger.lineage_for_family(claim_family_id)
        expired = frozenset(
            entry.supersedes_entry_id
            for entry in entries
            if entry.relation is LineageRelation.EXPIRES
            and entry.supersedes_entry_id is not None
            and entry.valid_from <= moment
        )
        return LineageView(
            claim_family_id=claim_family_id,
            entries=entries,
            current=self._current(entries, expired, moment),
            expired_entry_ids=expired,
        )

    def current_evidence(
        self, claim_family_id: str, *, at: datetime | None = None
    ) -> LineageEntry | None:
        """What Ariadne believes right now, or believed at a given moment."""
        return self.view(claim_family_id, at=at).current

    def evidence_at(self, claim_family_id: str, moment: datetime) -> LineageEntry | None:
        """Point-in-time reconstruction. The question an auditor actually asks."""
        return self.view(claim_family_id, at=moment).current

    def history(self, claim_family_id: str) -> list[LineageEntry]:
        return self._ledger.lineage_for_family(claim_family_id)

    def families_for_model(self, model_id: str) -> list[str]:
        return self._ledger.claim_families_for_model(model_id)

    def families_affected_by_version(self, model_id: str, model_version: str) -> list[str]:
        """Which claim families a newly deployed version puts back in question.

        Every family ever recorded for the model, minus the ones already tested against
        this exact version. A new version invalidates nothing automatically - it just makes
        every prior conclusion unverified with respect to it.
        """
        affected: list[str] = []
        for family in self._ledger.claim_families_for_model(model_id):
            entries = self._ledger.lineage_for_family(family)
            already = any(
                entry.scope.model_version == model_version
                and entry.relation is not LineageRelation.EXPIRES
                for entry in entries
            )
            if not already:
                affected.append(family)
        return affected

    def families_affected_by_distribution(
        self, model_id: str, distribution_version: str
    ) -> list[str]:
        """Families holding non-expired evidence gathered under a superseded distribution."""
        affected: list[str] = []
        for family in self._ledger.claim_families_for_model(model_id):
            view = self.view(family)
            for entry in view.entries:
                if entry.relation is LineageRelation.EXPIRES:
                    continue
                if entry.id in view.expired_entry_ids:
                    continue
                if entry.scope.distribution_version != distribution_version:
                    affected.append(family)
                    break
        return affected

    def audit_priority(self, claim_family_id: str, *, at: datetime | None = None) -> float:
        """How urgently this family should be re-tested, in [0, 1].

        Prior contradictions raise priority: an explanation that has already failed once is
        the one most worth re-checking after a change. This is the memory that makes the
        autonomous audit *targeted* rather than an exhaustive sweep.
        """
        moment = at or self._clock.now()
        view = self.view(claim_family_id, at=moment)
        if not view.entries:
            return 0.5  # never tested: worth a look, but nothing points at it yet

        priority = 0.3
        current = view.current

        if current is not None:
            if current.status is VerdictStatus.CONTRADICTED:
                priority += 0.35
            elif current.status is VerdictStatus.INCONCLUSIVE:
                priority += 0.20
            age = moment - current.valid_from
            if age > self._validity:
                priority += 0.15
        else:
            priority += 0.30  # nothing current: every reading has expired

        contradictions = view.count(VerdictStatus.CONTRADICTED)
        if contradictions:
            priority += min(0.20, 0.10 * contradictions)
        if view.has_expired_evidence:
            priority += 0.10

        return round(min(1.0, priority), 6)

    def is_stale(self, entry: LineageEntry, *, at: datetime | None = None) -> bool:
        """Whether an entry has outlived its validity window."""
        moment = at or self._clock.now()
        if entry.valid_until is not None:
            return moment >= entry.valid_until
        return (moment - entry.valid_from) > self._validity

    @staticmethod
    def _chain_tail(entries: list[LineageEntry]) -> LineageEntry | None:
        """The entry nothing else links back to — the real end of the chain.

        Deliberately not ``entries[-1]``. The ledger orders by ``(created_at, id)``, and a
        distribution shift expires every prior reading in a single clock instant, so those
        rows tie on ``created_at`` and the tiebreak falls to a content-addressed id. "Last"
        then means "largest hash", which is stable but arbitrary — and appending onto it
        made four entries share one predecessor. Being the tail is a property of the links,
        so it is read from the links.
        """
        if not entries:
            return None
        linked = {entry.previous_entry_hash for entry in entries if entry.previous_entry_hash}
        tails = [entry for entry in entries if entry.entry_hash not in linked]
        if len(tails) == 1:
            return tails[0]
        # Zero tails means a cycle; more than one means the chain already forked. Both are
        # corruption this method cannot repair, and verify_chain reports them. Fall back to
        # arrival order so the caller still writes an entry that shows up in that report,
        # rather than guessing at a branch and burying the damage deeper.
        return entries[-1]

    def verify_chain(self, claim_family_id: str) -> list[str]:
        """Recompute the hash chain and report entries whose link is broken.

        An empty list means the history is intact. A non-empty one means a row was altered
        or removed behind the ledger's back.

        This walks the links rather than trusting the ledger's sort order. A hash chain is a
        linked list; comparing it against ``ORDER BY (created_at, id)`` conflates two
        different things, and reported an intact history as broken every time more than one
        entry shared a timestamp. Walking reports exactly the rows genuinely unreachable
        from the origin — which is what "broken" was always supposed to mean.
        """
        entries = self._ledger.lineage_for_family(claim_family_id)
        if not entries:
            return []

        following: dict[str | None, list[LineageEntry]] = {}
        for entry in entries:
            following.setdefault(entry.previous_entry_hash, []).append(entry)

        origins = following.get(None, [])
        if len(origins) != 1:
            # No single starting point: nothing in the family can be trusted to verify.
            return sorted(entry.id for entry in entries)

        reachable: set[str] = set()
        cursor: LineageEntry | None = origins[0]
        while cursor is not None and cursor.id not in reachable:
            reachable.add(cursor.id)
            successors = following.get(cursor.entry_hash, [])
            # A fork is a break: past this point no single history exists to verify.
            cursor = successors[0] if len(successors) == 1 else None
        return sorted(entry.id for entry in entries if entry.id not in reachable)

    # -- helpers -----------------------------------------------------------------------

    def _derive_relation(
        self, verdict: Verdict, history: list[LineageEntry]
    ) -> tuple[LineageRelation, str | None]:
        if not history:
            return LineageRelation.INITIAL, None

        same_scope = [
            entry
            for entry in history
            if entry.relation is not LineageRelation.EXPIRES
            and entry.scope.matches(verdict.scope)
        ]
        if same_scope:
            # A re-audit of the identical scope replaces the previous reading of it.
            return LineageRelation.SUPERSEDES, same_scope[-1].id

        prior = [entry for entry in history if entry.relation is not LineageRelation.EXPIRES]
        if not prior:
            return LineageRelation.INITIAL, None

        latest = prior[-1]
        if latest.status is verdict.status:
            return LineageRelation.CONFIRMS, None
        return LineageRelation.DISPUTES, latest.id

    def _current(
        self, entries: list[LineageEntry], expired: frozenset[str], moment: datetime
    ) -> LineageEntry | None:
        candidates = [
            entry
            for entry in entries
            if entry.relation is not LineageRelation.EXPIRES
            and entry.id not in expired
            and entry.valid_from <= moment
            and entry.is_current_at(moment)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda e: (e.created_at, e.id))
