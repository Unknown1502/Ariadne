"""The hash chain must stay a chain when several entries share one instant.

A real defect, found by finally asserting the demo script's output instead of only checking
its exit code. `expire_evidence` writes one EXPIRES row per affected reading, all stamped
with the same `moment` — so a single distribution shift wrote four rows at one timestamp.

It re-read "the previous entry" as `lineage_for_family(...)[-1]` on each pass. That query
orders by `(created_at, id)`, and with `created_at` tied across the batch the tiebreak fell
to `id` — a *content-addressed hash*. "Last" therefore meant "largest hash", which is stable
but has nothing to do with insertion order, so every row after the first linked back to the
same predecessor. The result was a fork wearing a chain's clothes: four entries claiming one
parent, three of them unreachable from the origin.

Two failures came out of that, in opposite directions:

  - `verify_chain` compared links against sort order and reported *intact* history as
    broken — an integrity check crying wolf, which is how integrity checks stop being read.
  - The chain genuinely forked, so a whole branch could be dropped and the walk would never
    miss it. Tamper-evidence quietly weakened in the one structure the project points at
    when it claims history cannot be rewritten.

Nothing detected either, because every existing chain test spaced its audits thirty days
apart, where the tie never happens. These tests do the opposite: they collapse the
timestamps deliberately, and they check that genuine tampering is still caught.
"""

from __future__ import annotations

import pytest

from backend.core.clock import ManualClock
from backend.core.enums import LineageRelation
from backend.experiment_engine.runner import ExperimentRunner
from backend.lineage.service import LineageService
from backend.storage.sql import in_memory_ledger, lineage_table
from backend.verifier.verifier import generate_verdict
from tests.factories import T0, make_case

VERSIONS = ("1.0.0", "2.0.0", "3.0.0", "4.0.0")


@pytest.fixture
def env():
    ledger = in_memory_ledger()
    clock = ManualClock(T0)
    lineage = LineageService(ledger, clock=clock)
    runner = ExperimentRunner(clock=clock)

    def audit(version: str, distribution: str = "baseline_2024.1"):
        claim, plan = make_case(version, distribution)
        ledger.append_claim(claim)
        ledger.append_plan(plan)
        result = runner.run(plan, claim)
        ledger.append_evidence(result.evidence)
        verdict = generate_verdict(result.evidence, claim, plan, created_at=clock.now())
        ledger.append_verdict(verdict)
        return lineage.append_verdict(verdict, result.evidence)

    yield ledger, clock, lineage, audit
    ledger.dispose()


@pytest.fixture
def expired_batch(env):
    """Four readings, then a shift that expires all four in a single instant.

    This is the demo's own shape, and the shape the original bug needed: one
    `expire_evidence` call writing several rows that all carry the same `created_at`.
    """
    ledger, clock, lineage, audit = env
    family = None
    for version in VERSIONS:
        clock.advance(days=30)
        family = audit(version).claim_family_id
    clock.advance(days=1)
    appended = lineage.expire_evidence(family, reason="DISTRIBUTION_CHANGED")
    return ledger, clock, lineage, audit, family, appended


def links(entries):
    """previous_entry_hash -> the entries claiming it, i.e. the successor map."""
    out: dict[str | None, list] = {}
    for entry in entries:
        out.setdefault(entry.previous_entry_hash, []).append(entry)
    return out


class TestTheBatchStaysAChain:
    def test_the_shift_really_did_write_several_rows_at_one_timestamp(
        self, expired_batch
    ) -> None:
        """Guards the guard: if expiry stopped batching, these tests would pass vacuously."""
        *_, appended = expired_batch
        assert len(appended) > 1
        assert len({entry.created_at for entry in appended}) == 1, (
            "the regression needs a genuine timestamp tie to reproduce"
        )

    def test_no_two_entries_claim_the_same_predecessor(self, expired_batch) -> None:
        """The bug itself. Four rows shared one parent; a chain permits exactly one."""
        _, _, lineage, _, family, _ = expired_batch
        forked = {
            parent: [entry.id for entry in children]
            for parent, children in links(lineage.history(family)).items()
            if len(children) > 1
        }
        assert not forked, f"the hash chain forked: {forked}"

    def test_the_history_has_exactly_one_origin(self, expired_batch) -> None:
        _, _, lineage, _, family, _ = expired_batch
        origins = [e for e in lineage.history(family) if e.previous_entry_hash is None]
        assert len(origins) == 1

    def test_verify_chain_reports_the_batch_as_intact(self, expired_batch) -> None:
        """Before the fix this returned four ids for a history nobody had touched."""
        _, _, lineage, _, family, _ = expired_batch
        assert lineage.verify_chain(family) == []

    def test_every_entry_is_reachable_from_the_origin(self, expired_batch) -> None:
        _, _, lineage, _, family, _ = expired_batch
        entries = lineage.history(family)
        successors = links(entries)
        reachable, cursor = set(), next(e for e in entries if e.previous_entry_hash is None)
        while cursor is not None:
            reachable.add(cursor.id)
            following = successors.get(cursor.entry_hash, [])
            cursor = following[0] if following else None
        assert reachable == {entry.id for entry in entries}

    def test_a_later_verdict_chains_onto_the_last_expiry(self, expired_batch) -> None:
        """The batch's tail must be findable, or the next append forks again."""
        _, clock, lineage, audit, family, appended = expired_batch
        clock.advance(days=1)
        entry = audit("2.0.0", "shifted_2025.2")
        assert entry.previous_entry_hash == appended[-1].entry_hash
        assert lineage.verify_chain(family) == []

    def test_a_second_expiry_pass_also_stays_linear(self, expired_batch) -> None:
        _, clock, lineage, audit, family, _ = expired_batch
        clock.advance(days=1)
        audit("3.0.0", "shifted_2025.2")
        clock.advance(days=1)
        lineage.expire_evidence(family, reason="DISTRIBUTION_CHANGED")
        assert lineage.verify_chain(family) == []

    def test_the_expired_rows_are_still_expiries(self, expired_batch) -> None:
        """Relinking them must not have changed what they mean."""
        *_, appended = expired_batch
        assert all(entry.relation is LineageRelation.EXPIRES for entry in appended)
        assert all(entry.expired_reason == "DISTRIBUTION_CHANGED" for entry in appended)


class TestTamperingIsStillCaught:
    """A verification that never fails is worse than none — check it still can."""

    def test_a_removed_row_is_detected(self, expired_batch) -> None:
        ledger, _, lineage, _, family, _ = expired_batch
        victim = lineage.history(family)[2]
        with ledger.session() as session:
            session.execute(lineage_table.delete().where(lineage_table.c.id == victim.id))
        broken = lineage.verify_chain(family)
        assert broken, "deleting a row from the middle of the chain went unnoticed"
        assert victim.id not in broken  # it is gone; what breaks is everything after it

    def test_a_forged_link_is_detected(self, expired_batch) -> None:
        """Re-point a row at the origin, as someone excising history would."""
        ledger, _, lineage, _, family, _ = expired_batch
        entries = lineage.history(family)
        target = entries[3]
        with ledger.session() as session:
            session.execute(
                lineage_table.update()
                .where(lineage_table.c.id == target.id)
                .values(
                    document=target.model_copy(
                        update={"previous_entry_hash": entries[0].entry_hash}
                    ).model_dump_json()
                )
            )
        assert lineage.verify_chain(family), "a re-pointed link went unnoticed"

    def test_a_history_with_no_origin_reports_every_row(self, expired_batch) -> None:
        ledger, _, lineage, _, family, _ = expired_batch
        origin = next(e for e in lineage.history(family) if e.previous_entry_hash is None)
        with ledger.session() as session:
            session.execute(lineage_table.delete().where(lineage_table.c.id == origin.id))
        remaining = {entry.id for entry in lineage.history(family)}
        assert set(lineage.verify_chain(family)) == remaining

    def test_row_level_integrity_is_unaffected_by_the_fix(self, expired_batch) -> None:
        ledger, *_ = expired_batch
        assert ledger.verify_integrity("lineage_entries") == []


class TestTheTailIsReadFromTheLinks:
    def test_an_empty_family_has_no_tail(self) -> None:
        assert LineageService._chain_tail([]) is None

    def test_the_tail_is_the_row_nothing_points_at(self, expired_batch) -> None:
        """Not `entries[-1]`: with the batch tied on created_at, sort order is decided by a
        content hash, and the two answers disagree exactly when the bug used to bite."""
        _, _, lineage, _, family, appended = expired_batch
        entries = lineage.history(family)
        assert LineageService._chain_tail(entries).id == appended[-1].id

    def test_the_tail_survives_a_reversed_input_order(self, expired_batch) -> None:
        """The tail is a property of the links, so the caller's ordering cannot change it."""
        _, _, lineage, _, family, appended = expired_batch
        entries = lineage.history(family)
        assert LineageService._chain_tail(list(reversed(entries))).id == appended[-1].id

    def test_a_single_entry_is_its_own_tail(self, env) -> None:
        _, clock, _, audit = env
        clock.advance(days=30)
        entry = audit("1.0.0")
        assert LineageService._chain_tail([entry]).id == entry.id


def test_concurrent_appends_to_one_family_cannot_fork_the_chain(tmp_path):
    """Two writers racing for the same tail must produce a line, not a branch.

    The deployed ledger had a fork with exactly this shape: three entries stamped at one
    instant, and one parent hash claimed twice. The cause was not the timestamp tie that
    `_chain_tail` already handles - it was that reading the tail and writing its successor
    are separate statements, so both workers read the same tail and both appended to it.

    A single-threaded test cannot see this. Everything that is not under test is prepared
    serially first; the threads then contend for one thing only - the tail of one claim
    family - released from a barrier so their reads genuinely overlap.
    """
    import threading

    from backend.core.clock import ManualClock
    from backend.experiment_engine.runner import ExperimentRunner
    from backend.lineage.service import LineageService
    from backend.storage.sql import EvidenceLedger
    from backend.verifier.verifier import generate_verdict
    from tests.factories import T0, make_case

    ledger = EvidenceLedger(f"sqlite:///{tmp_path / 'race.db'}")
    clock = ManualClock(T0)
    runner = ExperimentRunner(clock=clock)

    # Distinct scopes inside one family, so the writes are genuinely different entries that
    # nonetheless compete for the same tail.
    scopes = [(v, d) for d in ("baseline_2024.1", "shifted_2025.2") for v in VERSIONS[:3]]
    prepared = []
    for version, distribution in scopes:
        claim, plan = make_case(version, distribution)
        result = runner.run(plan, claim)
        verdict = generate_verdict(result.evidence, claim, plan, created_at=clock.now())
        ledger.append_claim(claim)
        ledger.append_plan(plan)
        ledger.append_evidence(result.evidence)
        ledger.append_verdict(verdict)
        prepared.append((verdict, result.evidence))

    barrier = threading.Barrier(len(prepared))
    errors: list[BaseException] = []

    def append(verdict, evidence) -> None:
        try:
            barrier.wait(timeout=30)
            LineageService(ledger, clock=clock).append_verdict(verdict, evidence)
        except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
            errors.append(exc)

    threads = [threading.Thread(target=append, args=pair) for pair in prepared]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"a writer failed instead of retrying: {errors[0]!r}"

    family = prepared[0][0].claim_family_id
    entries = ledger.lineage_for_family(family)
    assert len(entries) == len(prepared), "an append was lost"

    parents = [e.previous_entry_hash for e in entries if e.previous_entry_hash]
    assert len(parents) == len(set(parents)), (
        "two entries name the same parent - the chain forked under concurrent append"
    )
    roots = [e for e in entries if e.previous_entry_hash is None]
    assert len(roots) == 1, f"a chain has exactly one origin, found {len(roots)}"

    # Structural: walking from the origin must reach every row that exists.
    by_parent = {e.previous_entry_hash: e for e in entries}
    walked, cursor = 0, by_parent.get(None)
    while cursor is not None:
        walked += 1
        cursor = by_parent.get(cursor.entry_hash)
    assert walked == len(entries), (
        f"walked {walked} of {len(entries)} entries; the rest are unreachable from the origin"
    )
    ledger.dispose()
