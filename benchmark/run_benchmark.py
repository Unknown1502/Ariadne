"""The Ariadne benchmark (prompt 13).

Runs every case through the real pipeline, scores the verdicts against ground truth derived
from the model formulas, and compares four configurations.

    python -m benchmark.run_benchmark --out var/benchmark

On the baselines: the docs suggest comparing against "a fixed test suite" and "a single-agent
workflow". Rather than build strawmen and score them, the comparison here is a set of
**ablations of Ariadne itself** - the same code with one mechanism removed:

    full            everything on
    no-control      the control arm is not run
    no-validity     the intervention-validity gate is removed
    self-report     the verdict is taken from the model's own explanation

Each answers a question a reviewer should ask: does the control earn its place, does the
validity gate prevent anything real, and is any of this better than believing the model?
Ablations are honest in a way a hand-written baseline is not, because every configuration
runs identical code against identical fixtures - the only variable is the mechanism.

`self-report` is worth naming precisely: it reads the explanation the target model ships and
concludes SUPPORTED, because that is what the explanation asserts. It is not a simulated
language model and no claim is made about how any real model would behave. It measures one
specific thing: what you get if you trust a system's self-description.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.core.clock import ManualClock
from backend.core.enums import VerdictStatus
from backend.core.errors import AriadneError
from backend.core.versions import (
    POLICY_VERSION,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    VERIFIER_VERSION,
)
from backend.experiment_engine.runner import ExperimentRunner
from backend.experiment_engine.target_model import (
    KNOWN_VERSIONS,
    UnstableTriageModel,
    describe_version,
    get_target_model,
)
from backend.lineage.service import LineageService
from backend.storage.sql import in_memory_ledger
from backend.verifier.verifier import verify
from benchmark.cases import CASES, BenchmarkCase, expected_distribution
from tests.factories import T0, make_claim, make_plan

CONFIGURATIONS = ("full", "no-control", "no-validity", "self-report")


@dataclass
class CaseResult:
    case_id: str
    category: str
    configuration: str
    expected: str
    observed: str
    correct: bool
    reason_codes: list[str] = field(default_factory=list)
    effect_size: float | None = None
    control_effect_size: float | None = None
    reproducibility: float | None = None
    intervention_validity: float | None = None
    latency_ms: float = 0.0
    note: str = ""


@dataclass
class Metrics:
    """Every metric prompt 13 asks for, plus the ones that make them interpretable."""

    cases: int = 0
    scored: int = 0
    correct: int = 0
    verdict_accuracy: float = 0.0
    false_support: int = 0
    false_support_rate: float = 0.0
    false_contradiction: int = 0
    false_contradiction_rate: float = 0.0
    inconclusive_expected: int = 0
    inconclusive_observed: int = 0
    inconclusive_calibration: float = 0.0
    mean_intervention_validity: float = 0.0
    mean_reproducibility: float = 0.0
    mean_latency_ms: float = 0.0
    no_verdict_expected: int = 0
    no_verdict_correct: int = 0


def build_case(case: BenchmarkCase, configuration: str):
    """Assemble the claim and plan for one case under one configuration."""
    claim = make_claim(case.model_version, case.distribution_version)

    if case.explanation != claim.source_explanation:
        # The compilation-behaviour cases carry their own explanation, so the claim shape
        # has to follow it rather than the default.
        primacy = "primary" in case.explanation.lower()
        claim = claim.model_copy(
            update={
                "source_explanation": case.explanation,
                "primacy_claim": primacy,
            }
        )

    overrides: dict[str, Any] = {
        key: value
        for key, value in case.plan_overrides.items()
        if key in ("repetitions", "min_repetitions_for_verdict", "min_effect_threshold")
    }
    plan = make_plan(claim, **overrides)

    if case.plan_overrides.get("weak_intervention"):
        from backend.core.enums import InterventionType
        from backend.core.schemas import InterventionSpec

        # Move urgency by 0.02 instead of to its neutral value: a probe too weak to learn
        # from, on a model where the claim is actually true.
        plan = plan.model_copy(
            update={
                "intervention": InterventionSpec(
                    variable="urgency_marker",
                    intervention_type=InterventionType.DECREASE,
                    delta=-0.02,
                )
            }
        )

    if configuration == "no-control":
        plan = plan.model_copy(update={"control": None})
    if configuration == "no-validity":
        plan = plan.model_copy(update={"validity_threshold": 0.0})

    return claim, plan


def run_case(case: BenchmarkCase, configuration: str) -> CaseResult:
    """Execute one case and score it against ground truth."""
    started = time.perf_counter()

    if case.expect_no_verdict:
        return score_no_verdict_case(case, configuration, started)

    claim, plan = build_case(case, configuration)
    unstable = bool(case.plan_overrides.get("unstable_model"))
    runner = ExperimentRunner(
        clock=ManualClock(T0),
        model_factory=(
            (lambda v, d: UnstableTriageModel(v)) if unstable else get_target_model
        ),
    )

    try:
        result = runner.run(plan, claim)
    except AriadneError as exc:
        return CaseResult(
            case_id=case.id, category=case.category, configuration=configuration,
            expected=str(case.expected), observed="ERROR", correct=False,
            note=f"{type(exc).__name__}: {exc}",
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    if configuration == "self-report":
        # Trust the model's own explanation. It asserts urgency is primary, so: SUPPORTED.
        observed = VerdictStatus.SUPPORTED
        outcome = None
        reasons = ["SELF_REPORTED"]
    else:
        # Verification thresholds may differ from execution thresholds, which is how a
        # tightened policy is applied to evidence that has already been collected.
        verify_plan = (
            plan.model_copy(update=case.verify_overrides) if case.verify_overrides else plan
        )
        outcome = verify(result.evidence, claim, verify_plan)
        observed = outcome.status
        reasons = outcome.reason_codes

    latency_ms = (time.perf_counter() - started) * 1000.0
    return CaseResult(
        case_id=case.id,
        category=case.category,
        configuration=configuration,
        expected=str(case.expected),
        observed=str(observed),
        correct=observed == case.expected,
        reason_codes=list(reasons),
        effect_size=outcome.effect_size if outcome else None,
        control_effect_size=outcome.control_effect_size if outcome else None,
        reproducibility=outcome.reproducibility if outcome else None,
        intervention_validity=outcome.intervention_validity if outcome else None,
        latency_ms=round(latency_ms, 3),
    )


def score_no_verdict_case(
    case: BenchmarkCase, configuration: str, started: float
) -> CaseResult:
    """Cases whose correct outcome is 'produce no verdict at all'."""
    from backend.agents.investigator import Investigator
    from backend.agents.llm import OfflineReasoner
    from backend.core.errors import UntestableExplanation
    from tests.factories import make_scope

    investigator = Investigator(OfflineReasoner(), clock=ManualClock(T0))
    observed = "VERDICT_PRODUCED"
    note = ""

    try:
        claim, _ = investigator.compile_claim(
            explanation=case.explanation,
            decision="HIGH_PRIORITY",
            scope=make_scope(case.model_version, case.distribution_version),
            investigation_id=f"INV-bench-{case.id}",
        )
        if claim.quarantined:
            observed = "NO_VERDICT"
            note = f"quarantined: {claim.quarantine_reasons}"
    except UntestableExplanation as exc:
        observed = "NO_VERDICT"
        note = str(exc)[:200]
    except AriadneError as exc:
        observed = "NO_VERDICT"
        note = f"{type(exc).__name__}: {exc}"[:200]

    return CaseResult(
        case_id=case.id, category=case.category, configuration=configuration,
        expected="NO_VERDICT", observed=observed, correct=observed == "NO_VERDICT",
        note=note, latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
    )


def compute_metrics(results: list[CaseResult]) -> Metrics:
    metrics = Metrics(cases=len(results))
    scored = [r for r in results if r.expected != "NO_VERDICT"]
    no_verdict = [r for r in results if r.expected == "NO_VERDICT"]

    metrics.scored = len(scored)
    metrics.correct = sum(1 for r in results if r.correct)
    metrics.verdict_accuracy = round(
        metrics.correct / len(results), 4
    ) if results else 0.0

    # A false support is the dangerous error: an explanation blessed that should not be.
    metrics.false_support = sum(
        1 for r in scored if r.observed == "SUPPORTED" and r.expected != "SUPPORTED"
    )
    metrics.false_contradiction = sum(
        1 for r in scored if r.observed == "CONTRADICTED" and r.expected != "CONTRADICTED"
    )
    metrics.false_support_rate = round(
        metrics.false_support / len(scored), 4
    ) if scored else 0.0
    metrics.false_contradiction_rate = round(
        metrics.false_contradiction / len(scored), 4
    ) if scored else 0.0

    metrics.inconclusive_expected = sum(1 for r in scored if r.expected == "INCONCLUSIVE")
    metrics.inconclusive_observed = sum(1 for r in scored if r.observed == "INCONCLUSIVE")
    correct_inconclusive = sum(
        1 for r in scored if r.expected == "INCONCLUSIVE" and r.observed == "INCONCLUSIVE"
    )
    metrics.inconclusive_calibration = round(
        correct_inconclusive / metrics.inconclusive_expected, 4
    ) if metrics.inconclusive_expected else 1.0

    validities = [r.intervention_validity for r in scored if r.intervention_validity is not None]
    reproducibilities = [r.reproducibility for r in scored if r.reproducibility is not None]
    metrics.mean_intervention_validity = round(
        sum(validities) / len(validities), 4
    ) if validities else 0.0
    metrics.mean_reproducibility = round(
        sum(reproducibilities) / len(reproducibilities), 4
    ) if reproducibilities else 0.0
    metrics.mean_latency_ms = round(
        sum(r.latency_ms for r in results) / len(results), 2
    ) if results else 0.0

    metrics.no_verdict_expected = len(no_verdict)
    metrics.no_verdict_correct = sum(1 for r in no_verdict if r.correct)
    return metrics


# --------------------------------------------------------------------------------------
# Reliability scenarios
# --------------------------------------------------------------------------------------


def run_reliability() -> dict[str, dict[str, Any]]:
    """Behavioural scenarios scored pass/fail rather than by verdict."""
    import asyncio
    import shutil
    import tempfile

    results: dict[str, dict[str, Any]] = {}
    workdir = Path(tempfile.mkdtemp())

    try:
        from backend.core.enums import InvestigationState
        from backend.events.bus import LocalEventBus
        from backend.runtime.orchestrator import build_pipeline
        from backend.runtime.worker import AriadneWorker, emit_model_version_deployed
        from backend.storage.runtime import LocalRuntimeStore

        async def scenarios() -> None:
            ledger = in_memory_ledger()
            clock = ManualClock(T0)
            runtime = LocalRuntimeStore(workdir / "runtime", clock=clock)
            lineage = LineageService(ledger, clock=clock)
            pipeline = build_pipeline(
                ledger=ledger, runtime=runtime, clock=clock, default_repetitions=12
            )
            bus = LocalEventBus(max_attempts=3, base_delay=0.0)
            worker = AriadneWorker(
                pipeline=pipeline, runtime=runtime, lineage=lineage, bus=bus, clock=clock
            )
            bus.subscribe(worker.handle)

            event = emit_model_version_deployed(
                model_id="synthetic-triage", model_version="1.0.0",
                distribution_version="baseline_2024.1", occurred_at=clock.now(),
            )
            await bus.publish(event)
            await bus.drain()
            baseline_counts = ledger.counts()

            # 1. duplicate delivery must not duplicate science
            for _ in range(5):
                await bus.publish_duplicate(event)
            await bus.drain()
            results["duplicate-event"] = {
                "passed": ledger.counts() == baseline_counts,
                "detail": f"{worker.stats.duplicates_skipped} duplicates suppressed; "
                          f"ledger unchanged",
            }

            # 2. a crashed worker resumes without re-running the experiment
            investigation = runtime.list_investigations()[0]
            runtime.save_investigation(
                investigation.model_copy(
                    update={"state": InvestigationState.EXPERIMENT_RUNNING}
                )
            )
            before = ledger.counts()["evidence"]
            resumed = pipeline.resume(investigation.id)
            results["worker-crash-recovery"] = {
                "passed": (
                    resumed.investigation.state
                    in (InvestigationState.COMPLETE, InvestigationState.REVIEW)
                    and ledger.counts()["evidence"] == before
                ),
                "detail": f"resumed from {resumed.resumed_from}, "
                          f"evidence rows unchanged at {before}",
            }

            # 3. malformed agent output must not produce a verdict
            from backend.agents.llm import OfflineReasoner

            broken_pipeline = build_pipeline(
                ledger=ledger, runtime=runtime, clock=clock,
                llm=OfflineReasoner(malformed=True), default_repetitions=12,
            )
            from backend.runtime.orchestrator import InvestigationRequest
            from tests.factories import make_scope

            broken = broken_pipeline.run(
                InvestigationRequest(
                    scope=make_scope("2.0.0"),
                    explanation="Urgency marker was the primary driver.",
                    decision="HIGH_PRIORITY", trigger_event_id="EVT-bench-malformed",
                    trigger_event_type="MODEL_VERSION_DEPLOYED",
                )
            )
            results["malformed-agent-output"] = {
                "passed": broken.verdict_status is None,
                "detail": f"state={broken.investigation.state}, no verdict produced",
            }

            # 4. a dead target model must raise, not fabricate
            from backend.core.errors import TargetModelError
            from backend.experiment_engine.target_model import FailingTargetModel

            claim, plan = build_case(CASES[0], "full")
            failing = ExperimentRunner(
                clock=ManualClock(T0), model_factory=lambda v, d: FailingTargetModel(v)
            )
            try:
                failing.run(plan, claim)
                passed, detail = False, "the runner returned a result from a dead model"
            except TargetModelError as exc:
                passed, detail = exc.retryable, f"raised {type(exc).__name__}, retryable"
            results["target-model-failure"] = {"passed": passed, "detail": detail}

            ledger.dispose()

        asyncio.run(scenarios())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return results


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------


def build_report() -> dict[str, Any]:
    by_configuration: dict[str, list[CaseResult]] = {}
    for configuration in CONFIGURATIONS:
        by_configuration[configuration] = [run_case(case, configuration) for case in CASES]

    reliability = run_reliability()

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "configuration": {
            "schema_version": SCHEMA_VERSION,
            "verifier_version": VERIFIER_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "policy_version": POLICY_VERSION,
            "seed": 20260101,
            "repetitions": 24,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "reasoner": "offline-deterministic-reasoner/1.0.0",
        },
        "target_models": [describe_version(v) for v in KNOWN_VERSIONS],
        "expected_distribution": expected_distribution(),
        "results": {
            configuration: [asdict(result) for result in results]
            for configuration, results in by_configuration.items()
        },
        "metrics": {
            configuration: asdict(compute_metrics(results))
            for configuration, results in by_configuration.items()
        },
        "reliability": reliability,
        "limitations": [
            "The target model is a hand-written formula over invented features. Results "
            "say nothing about any real model's explanations.",
            "Ground truth is derived from those formulas, so accuracy measures the "
            "verifier against a laboratory, not against the world.",
            "The ablations vary one mechanism at a time within Ariadne. They do not "
            "compare Ariadne against any other published system.",
            "`self-report` is a fixed rule that trusts the model's own explanation. It is "
            "not a language model, and it makes no claim about how one would behave.",
            "Explanation Debt weights are a policy choice; debt figures are not comparable "
            "across policy versions.",
            "Claim compilation runs on the offline deterministic reasoner by default, so "
            "these numbers do not measure Gemini's claim-extraction quality.",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append

    add("# Ariadne Benchmark Report\n")
    add(f"Generated: `{report['generated_at']}`\n")

    add("## Configuration\n")
    add("| Setting | Value |")
    add("|---|---|")
    for key, value in report["configuration"].items():
        add(f"| {key} | `{value}` |")
    add("")

    add("## Ground truth\n")
    add(
        "Derived from the published model formulas and fixed fixture seeds. No language "
        "model was consulted about the expected answers.\n"
    )
    add("| Version | Formula |")
    add("|---|---|")
    for model in report["target_models"]:
        add(f"| v{model['version']} | `{model['formula']}` |")
    add("")
    add(f"Expected verdict spread: `{report['expected_distribution']}`\n")

    add("## Results by configuration\n")
    add(
        "`full` is Ariadne. The others remove exactly one mechanism, so each column "
        "answers whether that mechanism earns its place.\n"
    )
    add(
        "| Configuration | Accuracy | False support | False contradiction | "
        "Inconclusive calibration | Mean validity |"
    )
    add("|---|---|---|---|---|---|")
    for configuration, metrics in report["metrics"].items():
        add(
            f"| `{configuration}` | {metrics['verdict_accuracy']:.0%} "
            f"({metrics['correct']}/{metrics['cases']}) "
            f"| {metrics['false_support']} ({metrics['false_support_rate']:.0%}) "
            f"| {metrics['false_contradiction']} "
            f"({metrics['false_contradiction_rate']:.0%}) "
            f"| {metrics['inconclusive_calibration']:.0%} "
            f"| {metrics['mean_intervention_validity']:.2f} |"
        )
    add("")

    add("## Case detail (full configuration)\n")
    add("| Case | Category | Expected | Observed | Correct | Reasons |")
    add("|---|---|---|---|---|---|")
    for result in report["results"]["full"]:
        mark = "yes" if result["correct"] else "**NO**"
        reasons = ", ".join(result["reason_codes"][:3]) or "-"
        add(
            f"| `{result['case_id']}` | {result['category']} | {result['expected']} "
            f"| {result['observed']} | {mark} | {reasons} |"
        )
    add("")

    add("## Reliability scenarios\n")
    add("| Scenario | Result | Detail |")
    add("|---|---|---|")
    for name, outcome in report["reliability"].items():
        add(
            f"| `{name}` | {'pass' if outcome['passed'] else '**FAIL**'} "
            f"| {outcome['detail']} |"
        )
    add("")

    add("## Limitations\n")
    for limitation in report["limitations"]:
        add(f"- {limitation}")
    add("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Ariadne benchmark.")
    parser.add_argument("--out", default="var/benchmark", help="output directory")
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="exit non-zero if the full configuration is not perfect",
    )
    args = parser.parse_args()

    report = build_report()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "benchmark.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    markdown = render_markdown(report)
    (out / "benchmark.md").write_text(markdown, encoding="utf-8")

    print(markdown)
    print(f"\nWrote {out / 'benchmark.json'} and {out / 'benchmark.md'}")

    full = report["metrics"]["full"]
    if args.fail_on_regression and full["correct"] != full["cases"]:
        failed = [r["case_id"] for r in report["results"]["full"] if not r["correct"]]
        print(f"\nREGRESSION: full configuration failed {failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
