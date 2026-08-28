"""The four-minute demo, as a script.

Runs the whole narrative headlessly so it can be checked in CI, rehearsed from a terminal,
and used as the reset baseline for the recorded demo. Every number printed is computed
during the run - nothing is hardcoded, and the script will happily print a result that
contradicts the story if the code stops working.

    python -m backend.scripts.run_demo

The story:

    1. A triage nurse sees a decision and an explanation.
    2. Ariadne compiles the explanation into a testable claim and probes it. v1 fails.
    3. A MODEL_VERSION_DEPLOYED event arrives. Nobody clicks anything.
    4. v2 supports the same explanation. v3 is genuinely ambiguous. v4 fails again.
    5. Data drifts. The evidence expires rather than being overwritten.
    6. Debt rises, and the Governor requires a human.
    7. A duplicate event and a worker crash change nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

from backend.core.clock import ManualClock
from backend.core.enums import InvestigationState
from backend.debt.calculator import explain
from backend.events.bus import LocalEventBus
from backend.experiment_engine.target_model import (
    STANDING_EXPLANATION,
    SYNTHETIC_DISCLAIMER,
    describe_version,
)
from backend.lineage.service import LineageService
from backend.runtime.orchestrator import build_pipeline
from backend.runtime.worker import (
    AriadneWorker,
    emit_distribution_changed,
    emit_model_version_deployed,
)
from backend.storage.runtime import LocalRuntimeStore
from backend.storage.sql import EvidenceLedger

MODEL_ID = "synthetic-triage"
BASELINE = "baseline_2024.1"
SHIFTED = "shifted_2025.2"

VERDICT_MARK = {"SUPPORTED": "[OK]", "CONTRADICTED": "[X]", "INCONCLUSIVE": "[?]"}


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def step(label: str) -> None:
    print(f"\n--- {label} ---")


class Demo:
    def __init__(self, var_dir: Path, repetitions: int) -> None:
        self.clock = ManualClock(datetime(2026, 1, 1, tzinfo=UTC))
        self.ledger = EvidenceLedger(f"sqlite:///{(var_dir / 'demo.sqlite3').as_posix()}")
        self.runtime = LocalRuntimeStore(var_dir / "runtime", clock=self.clock)
        self.lineage = LineageService(self.ledger, clock=self.clock)
        self.pipeline = build_pipeline(
            ledger=self.ledger,
            runtime=self.runtime,
            clock=self.clock,
            default_repetitions=repetitions,
        )
        self.bus = LocalEventBus(max_attempts=3, base_delay=0.0)
        self.worker = AriadneWorker(
            pipeline=self.pipeline, runtime=self.runtime, lineage=self.lineage,
            bus=self.bus, clock=self.clock, worker_id="worker-demo",
        )
        self.bus.subscribe(self.worker.handle)

    async def deploy(self, version: str, distribution: str = BASELINE, *, days: int = 30):
        self.clock.advance(days=days)
        await self.bus.publish(
            emit_model_version_deployed(
                model_id=MODEL_ID, model_version=version,
                distribution_version=distribution, occurred_at=self.clock.now(),
            )
        )
        await self.bus.drain()
        return self.latest()

    def latest(self):
        rows = self.runtime.list_investigations()
        return max(rows, key=lambda i: i.updated_at) if rows else None

    def family(self) -> str:
        families = self.lineage.families_for_model(MODEL_ID)
        return families[0] if families else ""

    def report(self, investigation) -> None:
        verdict = self.ledger.get_verdict(investigation.verdict_id or "")
        decision = self.ledger.get_decision(investigation.decision_id or "")
        if verdict is None:
            print(f"  state={investigation.state}  (no verdict: {investigation.last_error})")
            return
        mark = VERDICT_MARK[str(verdict.status)]
        print(
            f"  {mark} {verdict.status:14s} "
            f"effect={verdict.effect_size:+.4f}  "
            f"control={verdict.control_effect_size:+.4f}  "
            f"reproducibility={verdict.reproducibility:.2f}  "
            f"validity={verdict.intervention_validity:.2f}"
        )
        print(f"      reasons: {', '.join(verdict.reason_codes)}")
        if decision is not None:
            gate = " (awaiting human approval)" if decision.required_approval else ""
            print(f"      governor: {decision.action}{gate}")

    async def run(self) -> None:
        rule("ARIADNE - AI explanations that have to prove themselves")
        print(SYNTHETIC_DISCLAIMER)

        step("0:00  The unlikely hero")
        print("  A triage nurse sees the model's output:")
        print("      Decision:    HIGH PRIORITY")
        print(f'      Explanation: "{STANDING_EXPLANATION}"')
        print("  She is not an ML engineer. Ariadne tests whether that reason deserves trust.")

        step("0:20  The laboratory is open to inspection")
        for version in ("1.0.0", "2.0.0", "3.0.0", "4.0.0"):
            described = describe_version(version)
            print(f"  v{version}: {described['formula']}")
        print("  The same explanation ships with every version. Only the model changes.")

        step("0:45  v1 - compile the claim, run the probe")
        print("  IF urgency_marker is the primary driver,")
        print("  THEN neutralizing it while preserving the other features")
        print("  SHOULD lower the priority score.")
        investigation = await self.deploy("1.0.0")
        self.report(investigation)
        print("  Gemini proposed the test. Deterministic code decided the result.")

        step("1:40  A new model version is deployed. Nobody clicks Analyze.")
        for version in ("2.0.0", "3.0.0", "4.0.0"):
            print(f"\n  MODEL_VERSION_DEPLOYED  v{version}")
            investigation = await self.deploy(version)
            self.report(investigation)

        step("2:30  The claim's history across versions")
        family = self.family()
        view = self.lineage.view(family)
        for version, status in sorted(view.statuses_by_version.items()):
            print(f"  v{version}  {VERDICT_MARK[str(status)]} {status}")
        print(f"  Append-only rows: {len(view.entries)}")
        print(f"  Hash chain intact: {self.lineage.verify_chain(family) == []}")
        print(f"  Next audit priority: {self.lineage.audit_priority(family):.2f}")

        step("2:45  Point-in-time reconstruction")
        from datetime import timedelta

        origin = datetime(2026, 1, 1, tzinfo=UTC)
        for days in (35, 65, 95, 125):
            entry = self.lineage.evidence_at(family, origin + timedelta(days=days))
            answer = (
                f"v{entry.scope.model_version} {entry.status}" if entry else "no current evidence"
            )
            print(f"  What did we believe on day {days:>3}?  {answer}")

        step("3:10  The data distribution shifts")
        self.clock.advance(days=1)
        await self.bus.publish(
            emit_distribution_changed(
                model_id=MODEL_ID, distribution_version=SHIFTED,
                previous_distribution_version=BASELINE, occurred_at=self.clock.now(),
                drift_score=0.72, affected_features=["urgency_marker"],
            )
        )
        await self.bus.drain()
        print("  Evidence gathered on the old distribution is expired, not deleted.")
        print(f"  Current evidence: {self.lineage.current_evidence(family)}")
        print("  The old results stay exactly as true as they were, about the old data.")

        print("\n  Re-auditing v2 under the new distribution:")
        investigation = await self.deploy("2.0.0", SHIFTED, days=1)
        self.report(investigation)
        print("  The probe can no longer move the input enough to test the claim.")
        print("  INCONCLUSIVE is the honest answer. CONTRADICTED would be a fabrication.")

        step("3:20  Explanation Debt")
        snapshot = self.ledger.latest_debt(MODEL_ID)
        if snapshot is None:
            # Only reachable if every preceding investigation failed to reach the debt
            # stage. Saying so beats an AttributeError on stage, and beats printing a
            # confident zero for a number that was never computed.
            print("  No debt snapshot was recorded - no investigation reached debt scoring.")
        else:
            print("  " + explain(snapshot).replace("\n", "\n  "))
            print(
                "  Debt is a configurable operational risk score, not a scientific quantity."
            )

        step("3:30  Fleet resilience")
        before = self.ledger.counts()
        duplicate = emit_model_version_deployed(
            model_id=MODEL_ID, model_version="4.0.0",
            distribution_version=BASELINE, occurred_at=self.clock.now(),
        )
        for _ in range(3):
            await self.bus.publish_duplicate(duplicate)
        await self.bus.drain()
        after = self.ledger.counts()
        changed = {k: after[k] - before[k] for k in after if after[k] != before[k]}
        print(f"  3 duplicate deliveries -> new ledger rows: {changed or 'none'}")
        print(f"  duplicates suppressed by idempotency: {self.worker.stats.duplicates_skipped}")

        target = next(
            i for i in self.runtime.list_investigations()
            if i.state in (InvestigationState.COMPLETE, InvestigationState.REVIEW)
        )
        self.runtime.save_investigation(
            target.model_copy(update={"state": InvestigationState.EXPERIMENT_RUNNING})
        )
        rows_before = self.ledger.counts()["evidence"]
        resumed = self.pipeline.resume(target.id)
        print(
            f"  worker crash simulated mid-experiment -> resumed from "
            f"{resumed.resumed_from}, final state {resumed.investigation.state}"
        )
        print(
            f"  evidence rows before {rows_before}, after "
            f"{self.ledger.counts()['evidence']} (no duplicate science)"
        )

        step("3:50  What a reviewer can verify")
        print(f"  bus:        {self.bus.snapshot()}")
        print(f"  checkpoints:{self.runtime.stats()}")
        print(f"  ledger:     { {k: v for k, v in self.ledger.counts().items() if v} }")
        print(
            f"  integrity:  lineage_broken="
            f"{self.ledger.verify_integrity('lineage_entries')}  "
            f"verdicts_broken={self.ledger.verify_integrity('verdicts')}"
        )
        approvals = self.runtime.pending_approvals()
        print(f"  approvals awaiting a human: {len(approvals)}")

        rule("Ariadne makes explanations prove themselves across time, versions, and data")
        print(
            "Scope: behavioral explanation faithfulness under a declared intervention "
            "protocol.\nNot hidden causal truth. Not a clinical system."
        )

    def close(self) -> None:
        self.ledger.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Ariadne demo end to end.")
    parser.add_argument(
        "--var-dir", default="var/demo", help="where to write demo state (default: var/demo)"
    )
    parser.add_argument(
        "--repetitions", type=int, default=24, help="experiment repetitions (default: 24)"
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep previous demo state instead of resetting"
    )
    args = parser.parse_args()

    var_dir = Path(args.var_dir)
    if var_dir.exists() and not args.keep:
        shutil.rmtree(var_dir)
    var_dir.mkdir(parents=True, exist_ok=True)

    demo = Demo(var_dir, args.repetitions)
    try:
        asyncio.run(demo.run())
    finally:
        demo.close()


if __name__ == "__main__":
    main()
