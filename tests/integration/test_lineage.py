"""Lineage, provenance, and the append-only guarantee (prompt 07).

The claim these tests defend is a strong one: Ariadne's history cannot be quietly rewritten.
That is only worth asserting if it is enforced, so the suite attacks it directly - rewriting
rows, forging chains, and expiring evidence - and checks that the record survives.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from backend.core.clock import ManualClock
from backend.core.enums import LineageRelation, VerdictStatus
from backend.core.errors import AppendOnlyViolation, ValidationError
from backend.experiment_engine.runner import ExperimentRunner
from backend.lineage.service import LineageService
from backend.storage.sql import in_memory_ledger, lineage_table
from backend.verifier.verifier import generate_verdict
from tests.factories import T0, make_case

VERSIONS = ("1.0.0", "2.0.0", "3.0.0", "4.0.0")
EXPECTED = {
    "1.0.0": VerdictStatus.CONTRADICTED,
    "2.0.0": VerdictStatus.SUPPORTED,
    "3.0.0": VerdictStatus.INCONCLUSIVE,
    "4.0.0": VerdictStatus.CONTRADICTED,
}


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
        entry = lineage.append_verdict(verdict, result.evidence)
        return claim, verdict, result.evidence, entry

    yield ledger, clock, lineage, audit
    ledger.dispose()


@pytest.fixture
def four_versions(env):
    """The demo history: four audits, one per model version, thirty days apart."""
    ledger, clock, lineage, audit = env
    family = None
    for version in VERSIONS:
        clock.advance(days=30)
        claim, *_ = audit(version)
        family = claim.claim_family_id
    return ledger, clock, lineage, audit, family


class TestFourVersionHistory:
    def test_the_full_history_is_reconstructed(self, four_versions) -> None:
        _, _, lineage, _, family = four_versions
        view = lineage.view(family)
        assert len(view.entries) == 4
        assert {v: s for v, s in view.statuses_by_version.items()} == EXPECTED

    def test_the_first_entry_is_initial_and_the_rest_relate_to_it(self, four_versions) -> None:
        _, _, lineage, _, family = four_versions
        entries = lineage.history(family)
        assert entries[0].relation is LineageRelation.INITIAL
        assert entries[0].supersedes_entry_id is None
        for entry in entries[1:]:
            assert entry.relation is LineageRelation.DISPUTES
            assert entry.supersedes_entry_id is not None

    def test_a_reaudit_of_the_same_scope_supersedes(self, four_versions) -> None:
        _, clock, lineage, audit, family = four_versions
        clock.advance(days=1)
        *_, entry = audit("4.0.0")  # same version, same distribution, run again
        assert entry.relation is LineageRelation.SUPERSEDES
        assert entry.supersedes_entry_id is not None

    def test_a_matching_verdict_on_a_new_version_confirms(self, env) -> None:
        _, clock, lineage, audit = env
        clock.advance(days=1)
        claim, *_ = audit("1.0.0")
        clock.advance(days=1)
        *_, entry = audit("4.0.0")  # also CONTRADICTED
        assert entry.relation is LineageRelation.CONFIRMS
        assert lineage.view(claim.claim_family_id).count(VerdictStatus.CONTRADICTED) == 2

    def test_current_evidence_is_the_latest_reading(self, four_versions) -> None:
        _, _, lineage, _, family = four_versions
        current = lineage.current_evidence(family)
        assert current is not None
        assert current.scope.model_version == "4.0.0"
        assert current.status is VerdictStatus.CONTRADICTED


class TestPointInTime:
    def test_history_can_be_replayed_at_any_moment(self, four_versions) -> None:
        # "What did we believe in March?" has to have a real answer, not today's answer.
        _, _, lineage, _, family = four_versions
        for days, version in ((35, "1.0.0"), (65, "2.0.0"), (95, "3.0.0"), (125, "4.0.0")):
            entry = lineage.evidence_at(family, T0 + timedelta(days=days))
            assert entry is not None
            assert entry.scope.model_version == version

    def test_before_the_first_audit_nothing_is_current(self, four_versions) -> None:
        _, _, lineage, _, family = four_versions
        assert lineage.evidence_at(family, T0 + timedelta(days=1)) is None


class TestAppendOnly:
    def test_rewriting_a_row_with_different_content_is_refused(self, four_versions) -> None:
        ledger, _, lineage, _, family = four_versions
        entry = lineage.history(family)[0]
        forged = entry.model_copy(update={"status": VerdictStatus.SUPPORTED})
        with pytest.raises(AppendOnlyViolation, match="never rewritten"):
            ledger.append_lineage(forged)

    def test_reappending_identical_content_is_accepted(self, four_versions) -> None:
        # An at-least-once redelivery must be safe, so an identical re-append is a no-op.
        ledger, _, lineage, _, family = four_versions
        entry = lineage.history(family)[0]
        ledger.append_lineage(entry)
        assert len(lineage.history(family)) == 4

    def test_the_ledger_exposes_no_update_or_delete(self) -> None:
        from backend.storage.sql import EvidenceLedger

        surface = dir(EvidenceLedger)
        assert not [name for name in surface if name.startswith(("update_", "delete_", "remove_"))]

    def test_a_verdict_without_its_evidence_is_refused(self, env) -> None:
        _, clock, lineage, audit = env
        clock.advance(days=1)
        claim, verdict, evidence, _ = audit("1.0.0")
        orphan = verdict.model_copy(update={"evidence_ids": ["EVD-does-not-exist"]})
        with pytest.raises(ValidationError, match="does not reference evidence"):
            lineage.append_verdict(orphan, evidence)

    def test_mismatched_verdict_and_evidence_are_refused(self, env) -> None:
        _, clock, lineage, audit = env
        clock.advance(days=1)
        _, verdict, _, _ = audit("1.0.0")
        clock.advance(days=1)
        _, _, other_evidence, _ = audit("2.0.0")
        with pytest.raises(ValidationError, match="different claims"):
            lineage.append_verdict(verdict, other_evidence)


class TestHashChain:
    def test_an_intact_chain_reports_no_breaks(self, four_versions) -> None:
        _, _, lineage, _, family = four_versions
        assert lineage.verify_chain(family) == []
        assert lineage.history(family)[0].previous_entry_hash is None

    def test_each_entry_links_to_its_predecessor(self, four_versions) -> None:
        _, _, lineage, _, family = four_versions
        entries = lineage.history(family)
        for previous, current in zip(entries, entries[1:], strict=False):
            assert current.previous_entry_hash == previous.entry_hash

    def test_tampering_with_a_stored_row_is_detected(self, four_versions) -> None:
        # Bypass the ledger API entirely and edit the database, as an attacker would.
        ledger, _, lineage, _, family = four_versions
        target = lineage.history(family)[1]
        with ledger.session() as session:
            session.execute(
                lineage_table.update()
                .where(lineage_table.c.id == target.id)
                .values(document=target.model_copy(
                    update={"status": VerdictStatus.SUPPORTED}
                ).model_dump_json())
            )
        assert target.id in ledger.verify_integrity("lineage_entries")

    def test_an_untampered_ledger_passes_its_integrity_check(self, four_versions) -> None:
        ledger, *_ = four_versions
        assert ledger.verify_integrity("lineage_entries") == []
        assert ledger.verify_integrity("verdicts") == []


class TestExpiry:
    def test_expiring_evidence_appends_rather_than_deletes(self, four_versions) -> None:
        _, clock, lineage, _, family = four_versions
        before = len(lineage.history(family))
        clock.advance(days=1)
        expired = lineage.expire_evidence(
            family, reason="DISTRIBUTION_CHANGED", distribution_version="baseline_2024.1"
        )
        after = lineage.history(family)
        assert len(expired) == 4
        assert len(after) == before + 4
        # every original row is still there, untouched
        assert all(
            entry.relation is not LineageRelation.EXPIRES for entry in after[:before]
        )

    def test_expired_evidence_stops_being_current(self, four_versions) -> None:
        _, clock, lineage, _, family = four_versions
        clock.advance(days=1)
        lineage.expire_evidence(
            family, reason="DISTRIBUTION_CHANGED", distribution_version="baseline_2024.1"
        )
        assert lineage.current_evidence(family) is None
        assert lineage.view(family).has_expired_evidence

    def test_expiry_does_not_rewrite_the_past(self, four_versions) -> None:
        # The v2 result is still exactly as true as it was, about the world it measured.
        _, clock, lineage, _, family = four_versions
        clock.advance(days=1)
        lineage.expire_evidence(family, reason="DISTRIBUTION_CHANGED")
        historical = lineage.evidence_at(family, T0 + timedelta(days=65))
        assert historical is not None
        assert historical.status is VerdictStatus.SUPPORTED
        assert historical.scope.model_version == "2.0.0"

    def test_expiry_is_scoped_to_the_affected_distribution(self, env) -> None:
        _, clock, lineage, audit = env
        clock.advance(days=10)
        claim, *_ = audit("2.0.0", "baseline_2024.1")
        clock.advance(days=10)
        audit("2.0.0", "shifted_2025.2")
        family = claim.claim_family_id

        clock.advance(days=1)
        expired = lineage.expire_evidence(
            family, reason="DISTRIBUTION_CHANGED", distribution_version="baseline_2024.1"
        )
        assert len(expired) == 1
        assert expired[0].scope.distribution_version == "baseline_2024.1"
        current = lineage.current_evidence(family)
        assert current is not None
        assert current.scope.distribution_version == "shifted_2025.2"

    def test_expiring_twice_is_idempotent(self, four_versions) -> None:
        _, clock, lineage, _, family = four_versions
        clock.advance(days=1)
        first = lineage.expire_evidence(family, reason="DISTRIBUTION_CHANGED")
        second = lineage.expire_evidence(family, reason="DISTRIBUTION_CHANGED")
        assert len(first) == 4
        assert second == []


class TestAuditPrioritisation:
    def test_a_contradiction_raises_priority(self, env) -> None:
        # This is the memory that makes the autonomous audit targeted rather than a sweep.
        _, clock, lineage, audit = env
        clock.advance(days=1)
        claim, *_ = audit("2.0.0")  # SUPPORTED
        supported_priority = lineage.audit_priority(claim.claim_family_id)
        clock.advance(days=1)
        audit("1.0.0")  # CONTRADICTED
        contradicted_priority = lineage.audit_priority(claim.claim_family_id)
        assert contradicted_priority > supported_priority

    def test_an_untested_family_sits_in_the_middle(self, env) -> None:
        _, _, lineage, _ = env
        assert lineage.audit_priority("FAM-never-seen") == 0.5

    def test_expired_evidence_raises_priority(self, four_versions) -> None:
        _, clock, lineage, _, family = four_versions
        before = lineage.audit_priority(family)
        clock.advance(days=1)
        lineage.expire_evidence(family, reason="DISTRIBUTION_CHANGED")
        assert lineage.audit_priority(family) > before

    def test_priority_is_bounded(self, four_versions) -> None:
        _, clock, lineage, _, family = four_versions
        clock.advance(days=400)
        lineage.expire_evidence(family, reason="DISTRIBUTION_CHANGED")
        assert 0.0 <= lineage.audit_priority(family) <= 1.0


class TestAffectedFamilies:
    def test_a_new_version_reopens_untested_families(self, four_versions) -> None:
        _, _, lineage, _, family = four_versions
        affected = lineage.families_affected_by_version("synthetic-triage", "5.0.0")
        assert family in affected

    def test_an_already_tested_version_does_not_reopen(self, four_versions) -> None:
        _, _, lineage, _, _ = four_versions
        assert lineage.families_affected_by_version("synthetic-triage", "2.0.0") == []

    def test_families_resting_on_a_superseded_distribution_are_found(
        self, four_versions
    ) -> None:
        _, _, lineage, _, family = four_versions
        affected = lineage.families_affected_by_distribution(
            "synthetic-triage", "shifted_2025.2"
        )
        assert family in affected
