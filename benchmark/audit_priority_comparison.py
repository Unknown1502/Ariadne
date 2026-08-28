"""Does lineage-based audit priority actually reduce audit cost?

`docs/lineage-and-debt.md` and the README both claim that retrieving prior lineage lets
Ariadne prioritize re-audits rather than sweep exhaustively. `docs/limitations.md` names that
claim as unmeasured: *"The claim that memory makes auditing targeted rather than exhaustive
is stated in the README and is currently unmeasured — it needs a cost model and a comparison
against round-robin."* This is that comparison.

## Method

This calls the real, unmodified `LineageService.audit_priority()` against real
`LineageEntry` rows written through the real `EvidenceLedger.append_lineage()` — the same
persistence path production code uses. What is synthetic is the *population*: rather than
running the full Investigator -> Experimenter -> Verifier pipeline for hundreds of claim
families (which would test verdict accuracy, an already-answered question — see
`run_benchmark.py`), each family's history is generated directly from a seeded distribution
of outcomes. This isolates the one thing under test: given a known lineage state, does the
priority formula's *ranking* get the families worth re-testing audited sooner than a
schedule with no memory at all?

## Ground truth for "worth re-testing"

A family whose most recent verdict was CONTRADICTED, independent of whatever
`audit_priority()` itself says about it. This is not circular: CONTRADICTED-last-time is an
externally meaningful criterion on its own (it is literally the case the lineage docs use as
the motivating example — "urgency marker" going CONTRADICTED at v1), and it is one of five
inputs `audit_priority()` combines, not a copy of its output.

## Scenario

A `MODEL_VERSION_DEPLOYED` event makes every family "affected" (none has been tested against
the new version yet — the real semantics of `LineageService.families_affected_by_version`).
Only `K` audits can run per round. Two schedules consume that budget:

  - **lineage-priority**: highest `audit_priority()` first, computed once from the
    pre-deployment lineage (the same snapshot both schedules see).
  - **round-robin**: fixed rotation order, no memory of prior verdicts at all.

Metric: audits spent until every previously-contradicted family has been re-tested at least
once. Averaged over `--populations` independently seeded runs so the result is not one lucky
draw.

    python -m benchmark.audit_priority_comparison --out var/audit-priority
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.core.enums import LineageRelation, VerdictStatus
from backend.core.hashing import hash_chain, sha256_hex
from backend.core.ids import derive_id
from backend.core.schemas import LineageEntry, VersionScope
from backend.core.versions import PROTOCOL_VERSION, VERIFIER_VERSION
from backend.lineage.service import LineageService
from backend.storage.sql import in_memory_ledger

MODEL_ID = "synthetic-triage"
BASELINE_DISTRIBUTION = "baseline_2024.1"
T0 = datetime(2026, 1, 1, tzinfo=UTC)

# Roughly the operational mix the lineage docs assume: most claims survive, a meaningful
# minority do not, and a few sit unresolved. Not derived from a real deployment - there is
# no real deployment to derive it from - so it is a stated, adjustable assumption like every
# other threshold in this project, not a discovered constant.
OUTCOME_WEIGHTS: dict[VerdictStatus, float] = {
    VerdictStatus.SUPPORTED: 0.65,
    VerdictStatus.CONTRADICTED: 0.20,
    VerdictStatus.INCONCLUSIVE: 0.15,
}


@dataclass(frozen=True, slots=True)
class SyntheticFamily:
    family_id: str
    history_length: int
    final_status: VerdictStatus


def _entry(
    *,
    family_id: str,
    index: int,
    status: VerdictStatus,
    valid_from: datetime,
    relation: LineageRelation,
    supersedes_entry_id: str | None,
    previous_hash: str | None,
) -> LineageEntry:
    """Build one valid, hashed lineage row. Same shape the runtime writes, built by hand so
    hundreds of histories can be generated without running the full evidence pipeline."""
    scope = VersionScope(
        model_id=MODEL_ID, model_version="1.0.0", distribution_version=BASELINE_DISTRIBUTION
    )
    entry_id = derive_id("LIN", family_id, index)
    payload = {"family_id": family_id, "index": index, "status": str(status)}
    return LineageEntry(
        id=entry_id,
        claim_family_id=family_id,
        claim_id=derive_id("CLM", family_id, index),
        scope=scope,
        protocol_version=PROTOCOL_VERSION,
        verdict_id=derive_id("VDT", family_id, index),
        status=status,
        evidence_ids=[derive_id("EVD", family_id, index)],
        behavioral_support=0.8 if status is VerdictStatus.SUPPORTED else 0.2,
        intervention_validity=0.95,
        reproducibility=0.85 if status is VerdictStatus.SUPPORTED else 0.3,
        effect_size=-0.2 if status is VerdictStatus.SUPPORTED else -0.02,
        relation=relation,
        supersedes_entry_id=supersedes_entry_id,
        valid_from=valid_from,
        created_at=valid_from,
        input_hashes=[sha256_hex({"i": index})],
        output_hashes=[sha256_hex({"o": index})],
        verifier_version=VERIFIER_VERSION,
        previous_entry_hash=previous_hash,
        entry_hash=hash_chain(previous_hash, payload),
    )


def generate_population(*, size: int, seed: int, now: datetime) -> list[SyntheticFamily]:
    """A deterministic population of claim families with realistic-shaped histories."""
    rng = random.Random(seed)
    statuses = list(OUTCOME_WEIGHTS)
    weights = list(OUTCOME_WEIGHTS.values())
    families: list[SyntheticFamily] = []
    for i in range(size):
        final = rng.choices(statuses, weights=weights, k=1)[0]
        length = rng.randint(1, 3)
        families.append(
            SyntheticFamily(family_id=f"FAM-synthetic-{seed}-{i:04d}", history_length=length,
                             final_status=final)
        )
    return families


def populate_ledger(families: list[SyntheticFamily], *, now: datetime):
    """Write every family's history into a real, fresh in-memory ledger."""
    ledger = in_memory_ledger()
    for family in families:
        previous_hash: str | None = None
        # Earlier entries are SUPPORTED-leaning noise; only the final one carries the
        # family's ground-truth outcome, so a family with a longer history is not
        # artificially more "contradicted" than a short one.
        for index in range(family.history_length):
            is_final = index == family.history_length - 1
            status = family.final_status if is_final else VerdictStatus.SUPPORTED
            age_days = (family.history_length - index) * 20
            entry = _entry(
                family_id=family.family_id, index=index, status=status,
                valid_from=now - timedelta(days=age_days),
                relation=LineageRelation.INITIAL if index == 0 else LineageRelation.SUPERSEDES,
                supersedes_entry_id=(
                    None if index == 0 else derive_id("LIN", family.family_id, index - 1)
                ),
                previous_hash=previous_hash,
            )
            ledger.append_lineage(entry)
            previous_hash = entry.entry_hash
    return ledger


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    audits_to_full_coverage: int
    total_high_risk: int
    coverage_curve: list[float]  # fraction of high-risk families covered after each round


def simulate_schedule(
    *, order: list[str], high_risk: set[str], budget_per_round: int
) -> ScheduleResult:
    """Consume `order` in fixed batches of `budget_per_round`; track high-risk coverage."""
    covered: set[str] = set()
    curve: list[float] = []
    audits_spent = 0
    audits_to_full: int | None = None

    for start in range(0, len(order), budget_per_round):
        batch = order[start : start + budget_per_round]
        audits_spent += len(batch)
        covered.update(batch)
        curve.append(len(covered & high_risk) / len(high_risk) if high_risk else 1.0)
        if audits_to_full is None and high_risk <= covered:
            audits_to_full = audits_spent

    if audits_to_full is None:
        audits_to_full = audits_spent  # never fully covered within the whole population
    return ScheduleResult(
        audits_to_full_coverage=audits_to_full, total_high_risk=len(high_risk), coverage_curve=curve
    )


def run_one_population(
    *, size: int, budget_fraction: float, seed: int, now: datetime
) -> dict[str, Any]:
    families = generate_population(size=size, seed=seed, now=now)
    ledger = populate_ledger(families, now=now)
    lineage = LineageService(ledger)

    high_risk = {f.family_id for f in families if f.final_status is VerdictStatus.CONTRADICTED}
    budget = max(1, round(size * budget_fraction))

    # Both schedules see the identical pre-deployment snapshot; only the ordering differs.
    priority_order = sorted(
        (f.family_id for f in families),
        key=lambda fid: (-lineage.audit_priority(fid, at=now), fid),
    )
    round_robin_order = sorted(f.family_id for f in families)  # no memory: fixed, arbitrary

    priority_result = simulate_schedule(
        order=priority_order, high_risk=high_risk, budget_per_round=budget
    )
    round_robin_result = simulate_schedule(
        order=round_robin_order, high_risk=high_risk, budget_per_round=budget
    )

    # Where the high-risk families land in each ordering - the ranking-quality headline.
    priority_ranks = [priority_order.index(fid) for fid in high_risk] if high_risk else []
    round_robin_ranks = [round_robin_order.index(fid) for fid in high_risk] if high_risk else []
    n = len(families)

    ledger.dispose()
    return {
        "seed": seed,
        "population_size": size,
        "budget_per_round": budget,
        "high_risk_count": len(high_risk),
        "lineage_priority": {
            "audits_to_full_coverage": priority_result.audits_to_full_coverage,
            "coverage_curve": priority_result.coverage_curve,
            "mean_rank_percentile": (
                statistics.fmean(r / n for r in priority_ranks) if priority_ranks else None
            ),
        },
        "round_robin": {
            "audits_to_full_coverage": round_robin_result.audits_to_full_coverage,
            "coverage_curve": round_robin_result.coverage_curve,
            "mean_rank_percentile": (
                statistics.fmean(r / n for r in round_robin_ranks) if round_robin_ranks else None
            ),
        },
    }


def build_report(*, populations: int, size: int, budget_fraction: float) -> dict[str, Any]:
    runs = [
        run_one_population(size=size, budget_fraction=budget_fraction, seed=20260101 + i, now=T0)
        for i in range(populations)
    ]

    priority_audits = [r["lineage_priority"]["audits_to_full_coverage"] for r in runs]
    robin_audits = [r["round_robin"]["audits_to_full_coverage"] for r in runs]
    priority_pct = [
        r["lineage_priority"]["mean_rank_percentile"] for r in runs
        if r["lineage_priority"]["mean_rank_percentile"] is not None
    ]
    robin_pct = [
        r["round_robin"]["mean_rank_percentile"] for r in runs
        if r["round_robin"]["mean_rank_percentile"] is not None
    ]

    reduction = [
        (robin - pri) / robin if robin else 0.0
        for pri, robin in zip(priority_audits, robin_audits, strict=True)
        if robin > 0
    ]

    return {
        "config": {
            "populations": populations,
            "population_size": size,
            "budget_fraction": budget_fraction,
            "outcome_weights": {str(k): v for k, v in OUTCOME_WEIGHTS.items()},
            "generated_at": T0.isoformat(),
        },
        "summary": {
            "mean_audits_lineage_priority": round(statistics.fmean(priority_audits), 2),
            "mean_audits_round_robin": round(statistics.fmean(robin_audits), 2),
            "median_audit_reduction_pct": (
                round(statistics.median(reduction) * 100, 1) if reduction else None
            ),
            "mean_audit_reduction_pct": (
                round(statistics.fmean(reduction) * 100, 1) if reduction else None
            ),
            "mean_high_risk_rank_percentile_lineage_priority": (
                round(statistics.fmean(priority_pct) * 100, 1) if priority_pct else None
            ),
            "mean_high_risk_rank_percentile_round_robin": (
                round(statistics.fmean(robin_pct) * 100, 1) if robin_pct else None
            ),
        },
        "runs": runs,
        "limitations": [
            "The population is synthetically generated, not sampled from a real deployment - "
            "there is no real deployment to sample. The outcome-mix weights are a stated "
            "assumption, not a discovered constant; re-running with different weights is the "
            "honest way to check sensitivity, not a promise the ranking always wins by this "
            "margin.",
            "'High risk' is defined as 'most recent verdict was CONTRADICTED'. Real audit "
            "value also depends on business impact per claim, which this comparison has no "
            "model of.",
            "This measures scheduling efficiency given a known lineage state. It does not "
            "measure whether audit_priority's specific weights (0.35 for a contradicted "
            "current status, 0.20 for prior contradictions, etc.) are optimal - only that "
            "using lineage at all outperforms using none.",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    config = report["config"]
    summary = report["summary"]

    add("# Audit priority vs round-robin\n")
    add(
        f"{config['populations']} independently seeded populations of "
        f"{config['population_size']} synthetic claim families each, budget "
        f"{config['budget_fraction']:.0%} of the population per audit round.\n"
    )

    add("## Headline\n")
    add("| Metric | Lineage priority | Round-robin |")
    add("|---|---|---|")
    add(
        f"| Mean audits to full high-risk coverage | "
        f"{summary['mean_audits_lineage_priority']} | {summary['mean_audits_round_robin']} |"
    )
    add(
        f"| Mean rank percentile of high-risk families (lower = audited sooner) | "
        f"{summary['mean_high_risk_rank_percentile_lineage_priority']}% | "
        f"{summary['mean_high_risk_rank_percentile_round_robin']}% |"
    )
    add("")
    if summary["mean_audit_reduction_pct"] is not None:
        add(
            f"**Lineage-based prioritization reduced the audits needed to catch every "
            f"previously-contradicted family by a mean of "
            f"{summary['mean_audit_reduction_pct']}% "
            f"(median {summary['median_audit_reduction_pct']}%)** across "
            f"{config['populations']} populations.\n"
        )

    add("## Per-population detail\n")
    add("| Seed | High-risk families | Audits (priority) | Audits (round-robin) |")
    add("|---|---|---|---|")
    for run in report["runs"]:
        add(
            f"| {run['seed']} | {run['high_risk_count']} "
            f"| {run['lineage_priority']['audits_to_full_coverage']} "
            f"| {run['round_robin']['audits_to_full_coverage']} |"
        )
    add("")

    add("## Limitations\n")
    for limitation in report["limitations"]:
        add(f"- {limitation}")
    add("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare lineage-priority audit scheduling against round-robin."
    )
    parser.add_argument("--out", default="var/audit-priority", help="output directory")
    parser.add_argument("--populations", type=int, default=20)
    parser.add_argument("--size", type=int, default=200, help="claim families per population")
    parser.add_argument("--budget-fraction", type=float, default=0.05)
    args = parser.parse_args()

    report = build_report(
        populations=args.populations, size=args.size, budget_fraction=args.budget_fraction
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "audit-priority.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown = render_markdown(report)
    (out / "audit-priority.md").write_text(markdown, encoding="utf-8")

    print(markdown)
    print(f"\nWrote {out / 'audit-priority.json'} and {out / 'audit-priority.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
