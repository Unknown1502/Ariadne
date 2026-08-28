"""The agent audit trail.

These exist because the trail was silently empty for the whole build. `AgentInvocation` and
`ToolCall` objects were constructed and dropped on the floor while three docstrings claimed
every attempt was recorded, and the `audit_events` table had never held a row.

Two lessons are encoded here as tests:

  - Assert on the **stored** rows, not on the in-memory objects. The bug was precisely that
    the objects were fine and nothing persisted them.
  - Assert the sink is **wired into the real pipeline**, not merely that it works when
    called directly. The first fix passed a unit test and still wrote nothing end to end,
    because a swallowed exception hid a 100% failure rate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from backend.agents.audit import LedgerAuditSink, NullAuditSink
from backend.agents.investigator import Investigator
from backend.agents.llm import OfflineReasoner
from backend.core.clock import ManualClock
from backend.core.enums import RiskLevel
from backend.core.errors import PermissionDenied
from backend.runtime.orchestrator import InvestigationRequest, build_pipeline
from backend.storage.runtime import LocalRuntimeStore
from backend.storage.sql import audit_table, in_memory_ledger
from tests.factories import T0, make_scope


@pytest.fixture
def env(tmp_path: Path):
    ledger = in_memory_ledger()
    clock = ManualClock(T0)
    runtime = LocalRuntimeStore(tmp_path / "runtime", clock=clock)
    pipeline = build_pipeline(
        ledger=ledger, runtime=runtime, clock=clock, default_repetitions=8
    )
    yield ledger, clock, pipeline
    ledger.dispose()


def audit_rows(ledger) -> list[tuple[str, str | None]]:
    with ledger.session() as session:
        return [
            (row[0], row[1])
            for row in session.execute(
                select(audit_table.c.event_type, audit_table.c.agent_id)
            ).all()
        ]


def run_one(pipeline, version: str = "1.0.0"):
    return pipeline.run(
        InvestigationRequest(
            scope=make_scope(version),
            explanation="Urgency marker was the primary driver.",
            decision="HIGH_PRIORITY",
            trigger_event_id="EVT-audit",
            trigger_event_type="MODEL_VERSION_DEPLOYED",
        )
    )


class TestTrailIsPopulated:
    def test_an_investigation_leaves_an_audit_trail(self, env) -> None:
        ledger, _, pipeline = env
        run_one(pipeline)
        assert audit_rows(ledger), "audit_events is empty after a full investigation"

    def test_every_reasoning_agent_is_recorded(self, env) -> None:
        ledger, _, pipeline = env
        run_one(pipeline)
        agents = {agent for _, agent in audit_rows(ledger) if agent}
        assert {"investigator", "experimenter", "governor"} <= agents

    def test_the_verifier_leaves_no_agent_invocation(self, env) -> None:
        # It is not a reasoning agent and never calls a model, so it should have nothing
        # to record here. Its output is the verdict row itself.
        ledger, _, pipeline = env
        run_one(pipeline)
        agents = {agent for _, agent in audit_rows(ledger) if agent}
        assert "verifier" not in agents

    def test_tool_calls_are_recorded(self, env) -> None:
        ledger, _, pipeline = env
        run_one(pipeline)
        events = {event for event, _ in audit_rows(ledger)}
        assert "TOOL_CALL_ALLOWED" in events

    def test_successful_invocations_are_marked_as_such(self, env) -> None:
        ledger, _, pipeline = env
        run_one(pipeline)
        events = {event for event, _ in audit_rows(ledger)}
        assert "AGENT_INVOCATION_OK" in events


class TestFailuresAreRecorded:
    def test_a_retried_attempt_leaves_a_failure_row(self, tmp_path: Path) -> None:
        # A retry that eventually succeeds must not look like a clean first attempt.
        ledger = in_memory_ledger()
        clock = ManualClock(T0)
        runtime = LocalRuntimeStore(tmp_path / "runtime", clock=clock)
        pipeline = build_pipeline(
            ledger=ledger,
            runtime=runtime,
            clock=clock,
            llm=OfflineReasoner(fail_times=1),
            default_repetitions=8,
        )
        run_one(pipeline)
        events = [event for event, _ in audit_rows(ledger)]
        assert "AGENT_INVOCATION_FAILED" in events
        assert "AGENT_INVOCATION_OK" in events
        ledger.dispose()

    def test_a_denied_tool_call_is_recorded_before_the_exception(self, env) -> None:
        # The one event that would otherwise vanish entirely: the caller sees only an
        # exception, so if check_tool did not record it, nothing would.
        ledger, clock, _ = env
        agent = Investigator(
            OfflineReasoner(), clock=clock, audit=LedgerAuditSink(ledger)
        )
        with pytest.raises(PermissionDenied):
            agent.check_tool(
                "database.execute_sql", risk=RiskLevel.HIGH, investigation_id="INV-attack"
            )
        assert ("TOOL_CALL_DENIED", "investigator") in audit_rows(ledger)

    def test_a_risk_ceiling_breach_is_recorded(self, env) -> None:
        ledger, clock, _ = env
        agent = Investigator(
            OfflineReasoner(), clock=clock, audit=LedgerAuditSink(ledger)
        )
        with pytest.raises(PermissionDenied, match="capped at"):
            agent.check_tool(
                "lineage.get_family", risk=RiskLevel.HIGH, investigation_id="INV-attack"
            )
        assert ("TOOL_CALL_DENIED", "investigator") in audit_rows(ledger)


class TestSinkBehaviour:
    def test_a_broken_sink_does_not_abort_an_investigation(self, env) -> None:
        # The trail is best-effort; the evidence is not. A failed audit write must never
        # cost a completed scientific result.
        ledger, clock, _ = env

        class BrokenLedger:
            def append_audit(self, *args, **kwargs):
                raise RuntimeError("audit table unavailable")

        agent = Investigator(
            OfflineReasoner(), clock=clock, audit=LedgerAuditSink(BrokenLedger())
        )
        claim, _ = agent.compile_claim(
            explanation="Urgency marker was the primary driver.",
            decision="HIGH_PRIORITY",
            scope=make_scope(),
            investigation_id="INV-1",
        )
        assert claim.subject == "urgency_marker"

    def test_a_broken_sink_logs_rather_than_failing_silently(self, env, caplog) -> None:
        # Regression: a bare `except: pass` turned a 100% write failure into something
        # indistinguishable from an empty trail.
        ledger, clock, _ = env

        class BrokenLedger:
            def append_audit(self, *args, **kwargs):
                raise RuntimeError("audit table unavailable")

        agent = Investigator(
            OfflineReasoner(), clock=clock, audit=LedgerAuditSink(BrokenLedger())
        )
        with caplog.at_level("WARNING"), pytest.raises(PermissionDenied):
            agent.check_tool(
                "database.execute_sql", risk=RiskLevel.HIGH, investigation_id="INV-1"
            )
        assert any("audit write failed" in record.message for record in caplog.records)

    def test_the_null_sink_discards_without_error(self) -> None:
        agent = Investigator(
            OfflineReasoner(), clock=ManualClock(T0), audit=NullAuditSink()
        )
        call = agent.check_tool(
            "lineage.get_family", risk=RiskLevel.READ_ONLY, investigation_id="INV-1"
        )
        assert call.allowed

    def test_agents_are_constructible_without_a_sink(self) -> None:
        # Requiring persistence to build an agent would make agents hard to test.
        agent = Investigator(OfflineReasoner(), clock=ManualClock(T0))
        assert agent.check_tool(
            "lineage.get_family", risk=RiskLevel.READ_ONLY, investigation_id="INV-1"
        ).allowed


class TestLedgerIsStillAppendOnly:
    def test_audit_rows_survive_their_integrity_check(self, env) -> None:
        ledger, _, pipeline = env
        run_one(pipeline)
        assert ledger.verify_integrity("audit_events") == []

    def test_recording_the_same_event_twice_is_a_no_op(self, env) -> None:
        ledger, clock, _ = env
        sink = LedgerAuditSink(ledger)
        agent = Investigator(OfflineReasoner(), clock=clock, audit=sink)
        call = agent.check_tool(
            "lineage.get_family", risk=RiskLevel.READ_ONLY, investigation_id="INV-1"
        )
        before = len(audit_rows(ledger))
        sink.record_tool_call(call)
        assert len(audit_rows(ledger)) == before
