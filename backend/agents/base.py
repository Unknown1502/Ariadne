"""Agent scaffolding: bounded reasoning, typed output, recorded authority.

Every agent in Ariadne inherits three properties from this module.

**Bounded.** An agent gets ``loop_budget`` attempts to produce output that validates against
its declared schema. After that it is quarantined, not retried forever. An agent that cannot
produce valid output is a failure to surface, not a loop to hide in.

**Typed.** The model's response is parsed and validated before anything downstream sees it.
Whatever the model returns, what leaves this layer is either a valid domain object or an
exception.

**Recorded.** Every attempt writes an AgentInvocation - success or failure, with hashes,
latency, token counts, and cost estimate. An agent that failed silently would be
indistinguishable from one that was never called.

Authority is checked here too: an agent that calls a tool its manifest does not grant, or
writes a scope it does not hold, raises PermissionDenied rather than being trusted to
behave.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from backend.agents.audit import AuditSink, NullAuditSink
from backend.agents.llm import LLMClient, LLMRequest, extract_json
from backend.core.agent_contracts import AgentInvocation, AgentManifest, ToolCall
from backend.core.clock import Clock, SystemClock
from backend.core.enums import RiskLevel
from backend.core.errors import (
    AgentOutputError,
    AgentResponseRejected,
    AgentTimeout,
    LoopBudgetExceeded,
    PermissionDenied,
)
from backend.core.hashing import sha256_hex
from backend.core.ids import derive_id
from backend.core.schemas import AgentProvenance

T = TypeVar("T")


@dataclass
class ReasoningOutcome:
    """The result of one bounded reasoning call."""

    payload: dict[str, Any]
    provenance: AgentProvenance
    invocations: list[AgentInvocation] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def attempts(self) -> int:
        return len(self.invocations)


class AgentBase:
    """Shared behavior for the reasoning agents."""

    def __init__(
        self,
        manifest: AgentManifest,
        llm: LLMClient,
        *,
        clock: Clock | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self.manifest = manifest
        self._llm = llm
        self._clock = clock or SystemClock()
        self._audit = audit or NullAuditSink()

    # -- authority ---------------------------------------------------------------------

    def check_tool(
        self, tool: str, *, risk: RiskLevel, investigation_id: str, arguments: Any = None
    ) -> ToolCall:
        """Authorize a tool call against the manifest, recording the outcome either way.

        Denials are written to the audit sink before the exception is raised. A rejected
        privilege-escalation attempt is exactly the event a fleet operator wants in the
        trail, and it is the one that would otherwise vanish - the caller sees only an
        exception, so if this method did not record it, nothing would.
        """
        permission = self.manifest.tool(tool)
        now = self._clock.now()
        arguments_hash = sha256_hex(arguments if arguments is not None else {})

        def denied(reason: str) -> ToolCall:
            record = ToolCall(
                agent_id=self.manifest.agent_id, tool=tool,
                investigation_id=investigation_id, risk=risk, allowed=False,
                denied_reason=reason, arguments_hash=arguments_hash, called_at=now,
            )
            self._audit.record_tool_call(record)
            return record

        if permission is None:
            record = denied(f"{self.manifest.agent_id} has no grant for tool {tool!r}")
            raise PermissionDenied(record.denied_reason or "tool denied")

        if not permission.permits(risk):
            record = denied(
                f"tool {tool!r} is capped at {permission.max_risk} but the call requests "
                f"{risk}"
            )
            raise PermissionDenied(record.denied_reason or "tool denied")

        allowed = ToolCall(
            agent_id=self.manifest.agent_id, tool=tool, investigation_id=investigation_id,
            risk=risk, allowed=True, arguments_hash=arguments_hash, called_at=now,
        )
        self._audit.record_tool_call(allowed)
        return allowed

    def require_write(self, scope: str) -> None:
        if not self.manifest.may_write(scope):
            raise PermissionDenied(
                f"{self.manifest.agent_id} holds no write scope for {scope!r}; "
                f"granted: {self.manifest.write_scopes}"
            )

    def require_read(self, scope: str) -> None:
        if not self.manifest.may_read(scope):
            raise PermissionDenied(
                f"{self.manifest.agent_id} holds no read scope for {scope!r}; "
                f"granted: {self.manifest.read_scopes}"
            )

    # -- reasoning ---------------------------------------------------------------------

    def reason(
        self,
        request: LLMRequest,
        *,
        investigation_id: str,
        validate: Callable[[dict[str, Any]], T],
    ) -> tuple[T, ReasoningOutcome]:
        """Call the model until it returns output that validates, or run out of budget.

        `validate` does the semantic checking and raises on anything unacceptable. Its
        exception is treated as a retryable malformed response, which is what turns "the
        model returned nonsense" into a bounded, observable retry rather than a crash or a
        silently accepted bad value.
        """
        invocations: list[AgentInvocation] = []
        budget = self.manifest.loop_budget
        input_hash = sha256_hex({"system": request.system, "user": request.user})
        last_error: Exception | None = None

        for attempt in range(1, budget + 1):
            started = self._clock.now()
            try:
                response = self._llm.generate(
                    LLMRequest(
                        system=request.system,
                        user=request.user,
                        task=request.task,
                        context=request.context,
                        temperature=request.temperature,
                        max_output_tokens=request.max_output_tokens,
                        timeout_seconds=self.manifest.timeout_seconds,
                        response_schema=request.response_schema,
                    )
                )
                payload = extract_json(response.text)
                validated = validate(payload)
            except AgentResponseRejected as exc:
                # Deliberately not retried. A truncated or safety-blocked response is not a
                # flaky call: every agent runs at temperature 0, so the identical request
                # fails the identical way. Retrying would spend the budget and the money to
                # reproduce a certainty.
                #
                # Recorded before re-raising, because an agent failure that leaves no audit
                # row is indistinguishable from an agent that was never called - which is
                # exactly the defect that left this system's audit trail empty for its whole
                # first build.
                self._record(
                    self._invocation(
                        investigation_id=investigation_id,
                        attempt=attempt,
                        succeeded=False,
                        input_hash=input_hash,
                        started_at=started,
                        capability=request.task,
                        error_code=type(exc).__name__,
                        error_detail=str(exc)[:1000],
                    )
                )
                raise
            except (AgentOutputError, AgentTimeout, ValueError) as exc:
                last_error = exc
                invocations.append(
                    self._record(
                        self._invocation(
                            investigation_id=investigation_id,
                            attempt=attempt,
                            succeeded=False,
                            input_hash=input_hash,
                            started_at=started,
                            capability=request.task,
                            error_code=type(exc).__name__,
                            error_detail=str(exc)[:1000],
                        )
                    )
                )
                continue

            output_hash = sha256_hex(payload)
            invocations.append(
                self._record(
                    self._invocation(
                        investigation_id=investigation_id,
                        attempt=attempt,
                        succeeded=True,
                        input_hash=input_hash,
                        started_at=started,
                        capability=request.task,
                        output_hash=output_hash,
                        latency_ms=response.latency_ms,
                        llm_model=response.model,
                        prompt_tokens=response.prompt_tokens,
                        completion_tokens=response.completion_tokens,
                        estimated_cost_usd=response.estimated_cost_usd(),
                    )
                )
            )
            provenance = AgentProvenance(
                agent_id=self.manifest.agent_id,
                agent_version=self.manifest.version,
                role=str(self.manifest.role),
                llm_model=response.model,
                prompt_version=self.manifest.prompt_version,
                temperature=request.temperature,
                attempts=attempt,
                output_hash=output_hash,
                produced_at=started,
            )
            return validated, ReasoningOutcome(
                payload=payload, provenance=provenance, invocations=invocations
            )

        raise LoopBudgetExceeded(
            f"{self.manifest.agent_id} could not produce valid output for "
            f"{request.task!r} within {budget} attempts; last error: {last_error}"
        )

    def _record(self, invocation: AgentInvocation) -> AgentInvocation:
        """Persist an invocation and return it.

        Called on the failure path too. A retry that eventually succeeds would otherwise
        leave no trace of the attempts it took, and "this agent needed three tries" is
        exactly the signal that predicts a quarantine.
        """
        self._audit.record_invocation(invocation)
        return invocation

    def _invocation(
        self,
        *,
        investigation_id: str,
        attempt: int,
        succeeded: bool,
        input_hash: str,
        started_at: Any,
        capability: str,
        output_hash: str | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        latency_ms: float = 0.0,
        llm_model: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        estimated_cost_usd: float | None = None,
    ) -> AgentInvocation:
        return AgentInvocation(
            invocation_id=derive_id(
                "INVK", self.manifest.agent_id, investigation_id, capability, attempt
            ),
            agent_id=self.manifest.agent_id,
            agent_version=self.manifest.version,
            role=self.manifest.role,
            investigation_id=investigation_id,
            capability=capability,
            attempt=attempt,
            succeeded=succeeded,
            input_hash=input_hash,
            output_hash=output_hash,
            error_code=error_code,
            error_detail=error_detail,
            latency_ms=latency_ms,
            llm_model=llm_model or getattr(self._llm, "model_name", None),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_usd=estimated_cost_usd,
            started_at=started_at,
        )

    @property
    def uses_language_model(self) -> bool:
        """Whether the configured reasoner is an actual language model.

        Surfaced so the console can label a run honestly instead of showing a Gemini badge
        over the offline reasoner.
        """
        return bool(getattr(self._llm, "is_language_model", False))
