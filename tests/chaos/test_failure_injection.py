"""Failure injection (prompt 14).

Written from the hostile-reviewer position: assume every dependency will fail at the worst
moment and check that Ariadne degrades honestly rather than inventing a result.

The rule these tests enforce throughout: **a failure must never become a verdict.** When
something breaks, the acceptable outcomes are INCONCLUSIVE, an explicit failed state, or a
raised error. What is never acceptable is SUPPORTED or CONTRADICTED derived from a broken
run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.agents.investigator import Investigator
from backend.agents.llm import OfflineReasoner
from backend.core.clock import ManualClock
from backend.core.enums import InvestigationState, VerdictStatus
from backend.core.errors import (
    LoopBudgetExceeded,
    StorageError,
    TargetModelError,
)
from backend.events.bus import LocalEventBus
from backend.experiment_engine.runner import ExperimentRunner, InMemoryRunStore
from backend.experiment_engine.target_model import (
    FailingTargetModel,
    UnstableTriageModel,
    get_target_model,
)
from backend.lineage.service import LineageService
from backend.runtime.orchestrator import InvestigationRequest, build_pipeline
from backend.runtime.worker import AriadneWorker, emit_model_version_deployed
from backend.storage.runtime import LocalRuntimeStore
from backend.storage.sql import in_memory_ledger
from backend.verifier.verifier import ReasonCode, verify
from tests.factories import T0, make_case, make_scope

MODEL_ID = "synthetic-triage"
BASELINE = "baseline_2024.1"


@pytest.fixture
def env(tmp_path: Path):
    ledger = in_memory_ledger()
    clock = ManualClock(T0)
    runtime = LocalRuntimeStore(tmp_path / "runtime", clock=clock)
    lineage = LineageService(ledger, clock=clock)
    yield ledger, clock, runtime, lineage
    ledger.dispose()


def request_for(version: str = "1.0.0", distribution: str = BASELINE) -> InvestigationRequest:
    return InvestigationRequest(
        scope=make_scope(version, distribution),
        explanation="Urgency marker was the primary driver.",
        decision="HIGH_PRIORITY",
        trigger_event_id="EVT-chaos",
        trigger_event_type="MODEL_VERSION_DEPLOYED",
    )


class TestTargetModelFailure:
    def test_a_dead_target_model_raises_a_retryable_error(self) -> None:
        runner = ExperimentRunner(
            clock=ManualClock(T0), model_factory=lambda v, d: FailingTargetModel(v)
        )
        claim, plan = make_case("1.0.0")
        with pytest.raises(TargetModelError) as caught:
            runner.run(plan, claim)
        assert caught.value.retryable is True

    def test_a_dying_model_leaves_a_usable_checkpoint(self) -> None:
        store = InMemoryRunStore()
        calls = {"n": 0}
        real = get_target_model("1.0.0")

        class DiesHalfway:
            model_id, version = real.model_id, real.version
            distribution_version = real.distribution_version

            def predict(self, features):
                calls["n"] += 1
                if calls["n"] > 10:
                    raise RuntimeError("out of memory")
                return real.predict(features)

        runner = ExperimentRunner(
            clock=ManualClock(T0), run_store=store, model_factory=lambda v, d: DiesHalfway()
        )
        claim, plan = make_case("1.0.0", repetitions=8)
        with pytest.raises(TargetModelError):
            runner.run(plan, claim)

        recovered = ExperimentRunner(clock=ManualClock(T0), run_store=store).run(plan, claim)
        assert recovered.runs_reused > 0
        assert recovered.evidence.baseline.n == 8

    def test_an_unstable_model_yields_inconclusive_not_a_verdict(self) -> None:
        # A model that disagrees with itself cannot be probed by a paired design.
        runner = ExperimentRunner(
            clock=ManualClock(T0), model_factory=lambda v, d: UnstableTriageModel(v)
        )
        claim, plan = make_case("2.0.0")
        result = runner.run(plan, claim)
        outcome = verify(result.evidence, claim, plan)
        assert outcome.status is VerdictStatus.INCONCLUSIVE
        assert ReasonCode.MODEL_UNSTABLE in outcome.reason_codes


class TestReasonerFailure:
    def test_a_transient_malformed_response_is_retried(self) -> None:
        agent = Investigator(OfflineReasoner(fail_times=2), clock=ManualClock(T0))
        claim, outcome = agent.compile_claim(
            explanation="Urgency marker was the primary driver.",
            decision="HIGH_PRIORITY", scope=make_scope(), investigation_id="INV-1",
        )
        assert outcome.attempts == 3
        assert claim.subject == "urgency_marker"

    def test_persistent_malformed_output_exhausts_the_budget(self) -> None:
        agent = Investigator(OfflineReasoner(malformed=True), clock=ManualClock(T0))
        with pytest.raises(LoopBudgetExceeded):
            agent.compile_claim(
                explanation="Urgency marker was the primary driver.",
                decision="HIGH_PRIORITY", scope=make_scope(), investigation_id="INV-1",
            )

    def test_a_reasoner_timeout_does_not_hang(self) -> None:
        agent = Investigator(OfflineReasoner(hang=True), clock=ManualClock(T0))
        with pytest.raises(LoopBudgetExceeded):
            agent.compile_claim(
                explanation="Urgency marker was the primary driver.",
                decision="HIGH_PRIORITY", scope=make_scope(), investigation_id="INV-1",
            )

    def test_a_failed_agent_quarantines_the_investigation(self, env) -> None:
        # No verdict, no evidence, and a state that says plainly what happened.
        ledger, clock, runtime, lineage = env
        pipeline = build_pipeline(
            ledger=ledger, runtime=runtime, clock=clock, llm=OfflineReasoner(malformed=True)
        )
        result = pipeline.run(request_for())
        assert result.investigation.state is InvestigationState.QUARANTINED
        assert result.verdict_status is None
        assert ledger.counts()["verdicts"] == 0
        assert "AGENT_LOOP_BUDGET" in (result.investigation.last_error or "")

    def test_an_experimenter_failure_stops_before_execution(self, env) -> None:
        ledger, clock, runtime, lineage = env

        class BadPlanner(OfflineReasoner):
            def _plan_experiment(self, context):
                return {"intervention_type": "teleport", "target_variable": "x",
                        "control_variable": "y"}

        pipeline = build_pipeline(
            ledger=ledger, runtime=runtime, clock=clock, llm=BadPlanner()
        )
        result = pipeline.run(request_for())
        assert result.verdict_status is None
        assert ledger.counts()["evidence"] == 0


class TestStorageFailure:
    def test_a_ledger_failure_surfaces_rather_than_being_swallowed(self, env) -> None:
        ledger, clock, runtime, lineage = env

        class BrokenLedger:
            def __getattr__(self, name):
                def explode(*args, **kwargs):
                    raise StorageError("cloud sql is unreachable")

                return explode

        pipeline = build_pipeline(
            ledger=BrokenLedger(), runtime=runtime, clock=clock
        )
        with pytest.raises(StorageError):
            pipeline.run(request_for())

    def test_a_retryable_storage_error_keeps_the_checkpoint(self, env) -> None:
        # The retry must resume, so the checkpoint has to survive the failure.
        ledger, clock, runtime, lineage = env
        calls = {"n": 0}
        original = ledger.append_evidence

        def flaky(evidence):
            calls["n"] += 1
            if calls["n"] == 1:
                raise StorageError("transient write failure")
            return original(evidence)

        ledger.append_evidence = flaky  # type: ignore[method-assign]
        pipeline = build_pipeline(ledger=ledger, runtime=runtime, clock=clock)

        with pytest.raises(StorageError):
            pipeline.run(request_for())

        investigation = runtime.list_investigations()[0]
        assert investigation.claim_id  # progress before the failure was preserved

        recovered = pipeline.resume(investigation.id)
        assert recovered.investigation.state in (
            InvestigationState.COMPLETE, InvestigationState.REVIEW
        )


class TestWorkerFailure:
    async def test_a_crashed_worker_releases_its_claim_for_a_retry(self, env) -> None:
        ledger, clock, runtime, lineage = env

        class Exploding:
            def run(self, request):
                raise StorageError("worker died mid-pipeline")

        worker = AriadneWorker(
            pipeline=Exploding(), runtime=runtime, lineage=lineage, clock=clock,
            worker_id="worker-a",
        )
        event = emit_model_version_deployed(
            model_id=MODEL_ID, model_version="1.0.0",
            distribution_version=BASELINE, occurred_at=clock.now(),
        )
        with pytest.raises(StorageError):
            await worker.handle(event)

        # The claim is gone, so a second worker can pick the event up.
        assert runtime.get_idempotency(event.idempotency_key) is None

    async def test_a_second_worker_finishes_what_the_first_started(
        self, env, tmp_path: Path
    ) -> None:
        ledger, clock, runtime, lineage = env
        pipeline = build_pipeline(ledger=ledger, runtime=runtime, clock=clock)
        bus = LocalEventBus(max_attempts=3, base_delay=0.0)

        worker_a = AriadneWorker(
            pipeline=pipeline, runtime=runtime, lineage=lineage, bus=bus,
            clock=clock, worker_id="worker-a",
        )
        event = emit_model_version_deployed(
            model_id=MODEL_ID, model_version="1.0.0",
            distribution_version=BASELINE, occurred_at=clock.now(),
        )
        await worker_a.handle(event)

        investigation = runtime.list_investigations()[0]
        runtime.save_investigation(
            investigation.model_copy(update={"state": InvestigationState.VERIFICATION})
        )

        worker_b = AriadneWorker(
            pipeline=pipeline, runtime=runtime, lineage=lineage, bus=bus,
            clock=clock, worker_id="worker-b",
        )
        result = worker_b._pipeline.resume(investigation.id)
        assert result.investigation.state in (
            InvestigationState.COMPLETE, InvestigationState.REVIEW
        )
        assert ledger.counts()["evidence"] == 1  # not duplicated

    async def test_a_permanently_failing_event_is_dead_lettered(self, env) -> None:
        ledger, clock, runtime, lineage = env
        bus = LocalEventBus(max_attempts=3, base_delay=0.0)

        class AlwaysFails:
            def run(self, request):
                raise StorageError("permanent outage")

        worker = AriadneWorker(
            pipeline=AlwaysFails(), runtime=runtime, lineage=lineage, bus=bus, clock=clock
        )
        bus.subscribe(worker.handle)
        await bus.publish(
            emit_model_version_deployed(
                model_id=MODEL_ID, model_version="1.0.0",
                distribution_version=BASELINE, occurred_at=clock.now(),
            )
        )
        await bus.drain()

        assert len(bus.dead_letters) == 1
        assert bus.dead_letters[0].attempts == 3
        assert ledger.counts()["verdicts"] == 0


class TestDegradedScience:
    def test_too_few_runs_never_produces_a_verdict(self) -> None:
        claim, plan = make_case("2.0.0", repetitions=3, min_repetitions_for_verdict=3)
        result = ExperimentRunner(clock=ManualClock(T0)).run(plan, claim)
        stricter = plan.model_copy(update={"min_repetitions_for_verdict": 20})
        assert verify(result.evidence, claim, stricter).status is VerdictStatus.INCONCLUSIVE

    def test_a_broken_constraint_never_produces_a_verdict(self) -> None:
        claim, plan = make_case("1.0.0")
        result = ExperimentRunner(clock=ManualClock(T0)).run(plan, claim)
        invalid = result.evidence.model_copy(update={"validity_score": 0.0})
        outcome = verify(invalid, claim, plan)
        assert outcome.status is VerdictStatus.INCONCLUSIVE
        assert ReasonCode.INVALID_INTERVENTION in outcome.reason_codes

    def test_high_variance_produces_inconclusive_rather_than_a_guess(self) -> None:
        claim, plan = make_case("3.0.0")
        result = ExperimentRunner(clock=ManualClock(T0)).run(plan, claim)
        outcome = verify(result.evidence, claim, plan)
        assert outcome.status is VerdictStatus.INCONCLUSIVE

    def test_every_degraded_path_avoids_a_confident_verdict(self, env) -> None:
        # The summary property of this whole file.
        ledger, clock, runtime, lineage = env
        broken_reasoners = [
            OfflineReasoner(malformed=True),
            OfflineReasoner(hang=True),
            OfflineReasoner(fail_times=99),
        ]
        for reasoner in broken_reasoners:
            pipeline = build_pipeline(
                ledger=ledger, runtime=runtime, clock=clock, llm=reasoner
            )
            result = pipeline.run(request_for())
            assert result.verdict_status is None
            assert result.investigation.state in (
                InvestigationState.QUARANTINED, InvestigationState.FAILED
            )
