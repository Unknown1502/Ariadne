"""Tests for the audit-priority vs round-robin comparison.

This exercises the real, unmodified `LineageService.audit_priority()` against real
`LineageEntry` rows in a real ledger — the same objects and persistence path production
code uses. What is synthetic is only the population of claim-family histories, and these
tests check that population is built the way its own docstring claims.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.core.enums import VerdictStatus
from benchmark.audit_priority_comparison import (
    OUTCOME_WEIGHTS,
    build_report,
    generate_population,
    populate_ledger,
    run_one_population,
    simulate_schedule,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class TestPopulationGeneration:
    def test_deterministic_for_a_fixed_seed(self) -> None:
        first = generate_population(size=50, seed=1, now=T0)
        second = generate_population(size=50, seed=1, now=T0)
        assert first == second

    def test_a_different_seed_gives_a_different_population(self) -> None:
        first = generate_population(size=50, seed=1, now=T0)
        second = generate_population(size=50, seed=2, now=T0)
        assert first != second

    def test_family_ids_are_unique(self) -> None:
        families = generate_population(size=100, seed=1, now=T0)
        assert len({f.family_id for f in families}) == 100

    def test_outcome_mix_is_roughly_the_declared_weights(self) -> None:
        # A large sample should land close to the declared distribution; this is a sanity
        # check on the generator, not a claim about any real deployment.
        families = generate_population(size=2000, seed=1, now=T0)
        contradicted = sum(1 for f in families if f.final_status is VerdictStatus.CONTRADICTED)
        rate = contradicted / len(families)
        assert abs(rate - OUTCOME_WEIGHTS[VerdictStatus.CONTRADICTED]) < 0.05


class TestPopulateLedger:
    def test_every_family_gets_a_readable_history(self) -> None:
        families = generate_population(size=20, seed=1, now=T0)
        ledger = populate_ledger(families, now=T0)
        try:
            for family in families:
                history = ledger.lineage_for_family(family.family_id)
                assert len(history) == family.history_length
                assert history[-1].status == family.final_status
        finally:
            ledger.dispose()

    def test_histories_are_hash_chained(self) -> None:
        families = [f for f in generate_population(size=30, seed=1, now=T0) if f.history_length > 1]
        assert families, "need at least one multi-entry family to test chaining"
        ledger = populate_ledger(families, now=T0)
        try:
            history = ledger.lineage_for_family(families[0].family_id)
            for earlier, later in zip(history, history[1:], strict=False):
                assert later.previous_entry_hash == earlier.entry_hash
                assert later.supersedes_entry_id == earlier.id
        finally:
            ledger.dispose()

    def test_the_real_ledger_passes_integrity_verification(self) -> None:
        # If the synthetic writer produced a broken chain, this would catch it - the same
        # check the runtime uses to detect real tampering.
        families = generate_population(size=40, seed=1, now=T0)
        ledger = populate_ledger(families, now=T0)
        try:
            assert ledger.verify_integrity() == []
        finally:
            ledger.dispose()


class TestScheduleSimulation:
    def test_full_coverage_is_reached_when_the_budget_covers_everyone(self) -> None:
        result = simulate_schedule(
            order=["a", "b", "c"], high_risk={"a", "c"}, budget_per_round=3
        )
        assert result.audits_to_full_coverage == 3
        assert result.coverage_curve[-1] == 1.0

    def test_priority_ordering_reaches_high_risk_families_sooner(self) -> None:
        # High-risk families placed first must finish before the same families placed last.
        early = simulate_schedule(
            order=["risk", "a", "b", "c"], high_risk={"risk"}, budget_per_round=1
        )
        late = simulate_schedule(
            order=["a", "b", "c", "risk"], high_risk={"risk"}, budget_per_round=1
        )
        assert early.audits_to_full_coverage < late.audits_to_full_coverage

    def test_no_high_risk_families_is_full_coverage_immediately(self) -> None:
        result = simulate_schedule(order=["a", "b"], high_risk=set(), budget_per_round=1)
        assert result.coverage_curve == [1.0, 1.0]


class TestEndToEndComparison:
    def test_lineage_priority_never_needs_more_audits_than_round_robin_on_average(self) -> None:
        """The claim under test, computed rather than asserted.

        Not a >= bound on every single seed (a synthetic population could occasionally
        favour round-robin by chance), but the mean over many independent populations must
        not regress into "memory doesn't help" - if it ever does, the README's claim needs
        to be corrected, not this test loosened.
        """
        report = build_report(populations=10, size=100, budget_fraction=0.05)
        summary = report["summary"]
        assert summary["mean_audits_lineage_priority"] <= summary["mean_audits_round_robin"]
        assert summary["mean_audit_reduction_pct"] is not None
        assert summary["mean_audit_reduction_pct"] > 0

    def test_high_risk_families_rank_higher_under_lineage_priority(self) -> None:
        report = build_report(populations=5, size=100, budget_fraction=0.05)
        summary = report["summary"]
        assert (
            summary["mean_high_risk_rank_percentile_lineage_priority"]
            < summary["mean_high_risk_rank_percentile_round_robin"]
        )

    def test_round_robin_percentile_is_close_to_uniform(self) -> None:
        # A schedule with no memory should place high-risk families near the population
        # median rank, not systematically early or late - this is the sanity check that the
        # comparison's "no memory" baseline is actually memoryless.
        report = build_report(populations=10, size=200, budget_fraction=0.05)
        assert 40.0 < report["summary"]["mean_high_risk_rank_percentile_round_robin"] < 60.0

    def test_report_is_reproducible(self) -> None:
        first = build_report(populations=3, size=50, budget_fraction=0.1)
        second = build_report(populations=3, size=50, budget_fraction=0.1)
        assert first["summary"] == second["summary"]

    def test_single_population_run_is_self_consistent(self) -> None:
        run = run_one_population(size=80, budget_fraction=0.05, seed=42, now=T0)
        assert run["lineage_priority"]["audits_to_full_coverage"] <= run["population_size"]
        assert run["round_robin"]["audits_to_full_coverage"] <= run["population_size"]

    @pytest.mark.parametrize("budget_fraction", [0.01, 0.1, 0.25])
    def test_the_advantage_holds_across_different_budgets(self, budget_fraction: float) -> None:
        report = build_report(populations=5, size=150, budget_fraction=budget_fraction)
        summary = report["summary"]
        assert summary["mean_audits_lineage_priority"] <= summary["mean_audits_round_robin"]
