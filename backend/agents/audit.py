"""The agent audit trail.

Every agent invocation and every tool-permission decision is written here. Two of those
records only exist if something deliberately writes them:

  - A **failed invocation** is invisible to the caller by the time a retry succeeds, so
    without this an agent that took three attempts looks identical to one that took one.
  - A **denied tool call** surfaces to the caller as an exception and nothing else. If it
    were not recorded at the point of denial, a rejected privilege-escalation attempt would
    leave no trace at all - which is the one event a fleet operator most wants to see.

``NullAuditSink`` exists so agents remain constructible without a ledger, in unit tests and
in the offline demo. It is a deliberate no-op rather than an accident: an agent that cannot
be built without persistence is an agent that is hard to test.
"""

from __future__ import annotations

import logging
from typing import Protocol

from backend.core.agent_contracts import AgentInvocation, AgentMessage, ToolCall
from backend.core.ids import derive_id
from backend.storage.sql import EvidenceLedger

logger = logging.getLogger("ariadne.audit")


class AuditSink(Protocol):
    """Where agent activity is recorded."""

    def record_invocation(self, invocation: AgentInvocation) -> None: ...
    def record_tool_call(self, call: ToolCall) -> None: ...
    def record_message(self, message: AgentMessage) -> None: ...


class NullAuditSink:
    """Discards records. For unit tests and agents constructed without a ledger."""

    def record_invocation(self, invocation: AgentInvocation) -> None:
        return None

    def record_tool_call(self, call: ToolCall) -> None:
        return None

    def record_message(self, message: AgentMessage) -> None:
        return None


class LedgerAuditSink:
    """Writes agent activity to the append-only ledger's audit_events table.

    Failures are swallowed rather than raised: an audit write that aborted an otherwise
    successful investigation would trade a complete scientific result for a complete log,
    which is the wrong way round. The trail is best-effort; the evidence is not.

    They are *logged*, though. A bare ``except: pass`` here previously turned a 100% write
    failure into something indistinguishable from an empty trail - the table stayed empty
    and nothing anywhere said why.
    """

    def __init__(self, ledger: EvidenceLedger) -> None:
        self._ledger = ledger

    def record_invocation(self, invocation: AgentInvocation) -> None:
        self._append(
            invocation,
            identifier=invocation.invocation_id,
            event_type=(
                "AGENT_INVOCATION_OK" if invocation.succeeded else "AGENT_INVOCATION_FAILED"
            ),
            occurred_at=invocation.started_at,
            investigation_id=invocation.investigation_id,
            agent_id=invocation.agent_id,
        )

    def record_tool_call(self, call: ToolCall) -> None:
        self._append(
            call,
            identifier=derive_id(
                "TOOL", call.agent_id, call.tool, call.investigation_id,
                call.arguments_hash, call.called_at.isoformat(),
            ),
            event_type="TOOL_CALL_ALLOWED" if call.allowed else "TOOL_CALL_DENIED",
            occurred_at=call.called_at,
            investigation_id=call.investigation_id,
            agent_id=call.agent_id,
        )

    def record_message(self, message: AgentMessage) -> None:
        self._append(
            message,
            identifier=message.message_id,
            event_type="AGENT_HANDOFF",
            occurred_at=message.created_at,
            investigation_id=message.investigation_id,
            agent_id=message.from_agent,
        )

    def _append(self, record, **columns) -> None:
        try:
            self._ledger.append_audit(record, **columns)
        except Exception:
            logger.warning(
                "audit write failed for %s; the investigation continues",
                columns.get("event_type"),
                exc_info=True,
            )
