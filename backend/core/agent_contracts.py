"""Fleet governance contracts: identity, permission, and typed handoff.

These types are what make "four agents" an enforced boundary rather than a slide. An agent
declares, in a versioned manifest, what it may read, what it may write, which tools it may
call, and how much risk it may reach. The router refuses handoffs the manifest does not
support, and the permission check refuses tool calls the manifest does not grant.

A confused deputy needs two things: authority it did not earn, and no record of using it.
This module removes both.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, SerializeAsAny, field_validator, model_validator

from backend.core.enums import AgentRole, RiskLevel
from backend.core.schemas import SEMVER_RE, AriadneModel

RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.READ_ONLY: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
}


class ToolPermission(AriadneModel):
    """One tool an agent may call, and the ceiling on what that call may do."""

    tool: str = Field(min_length=1, max_length=128)
    max_risk: RiskLevel = RiskLevel.READ_ONLY
    rate_limit_per_investigation: int = Field(default=10, ge=1, le=1000)

    def permits(self, risk: RiskLevel) -> bool:
        return RISK_ORDER[risk] <= RISK_ORDER[self.max_risk]


class AgentManifest(AriadneModel):
    """A versioned declaration of one agent's identity and authority.

    ``read_scopes`` and ``write_scopes`` are resource names, not free text. The Investigator
    holds no write scope on evidence; the Experimenter holds none on verdicts; the Governor
    holds none on either. Those absences are the design.
    """

    agent_id: str = Field(min_length=1, max_length=64)
    version: str
    role: AgentRole
    owner: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=500)

    capabilities: list[str] = Field(min_length=1, max_length=16)
    input_schema: str = Field(min_length=1)
    output_schema: str = Field(min_length=1)

    read_scopes: list[str] = Field(default_factory=list, max_length=32)
    write_scopes: list[str] = Field(default_factory=list, max_length=32)
    allowed_tools: list[ToolPermission] = Field(default_factory=list, max_length=32)
    max_risk_level: RiskLevel = RiskLevel.READ_ONLY

    loop_budget: int = Field(default=3, ge=1, le=10)
    """Bounded retries. An agent that cannot produce valid output within its budget is
    quarantined rather than allowed to spin."""

    timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    service_account: str | None = None
    healthy: bool = True
    prompt_version: str | None = None
    uses_llm: bool = True

    @field_validator("version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not SEMVER_RE.match(v):
            raise ValueError(f"agent version must be MAJOR.MINOR.PATCH, got {v!r}")
        return v

    @model_validator(mode="after")
    def _authority_is_bounded(self) -> AgentManifest:
        for tool in self.allowed_tools:
            if RISK_ORDER[tool.max_risk] > RISK_ORDER[self.max_risk_level]:
                raise ValueError(
                    f"tool {tool.tool!r} is granted {tool.max_risk} but agent "
                    f"{self.agent_id!r} is capped at {self.max_risk_level}"
                )
        if self.role is AgentRole.VERIFIER and self.uses_llm:
            raise ValueError(
                "the Verifier must not use an LLM: its verdict is the one output that has "
                "to be reproducible from the evidence alone"
            )
        overlap = set(self.write_scopes) & {"evidence.raw", "verdict", "lineage"}
        if self.role in (AgentRole.INVESTIGATOR, AgentRole.EXPERIMENTER) and "verdict" in overlap:
            raise ValueError(f"{self.role} must not hold a write scope on verdicts")
        if self.role is AgentRole.GOVERNOR and overlap:
            raise ValueError(
                f"the Governor must not write evidence or verdicts; found {sorted(overlap)}"
            )
        return self

    def tool(self, name: str) -> ToolPermission | None:
        return next((t for t in self.allowed_tools if t.tool == name), None)

    def may_read(self, scope: str) -> bool:
        return scope in self.read_scopes

    def may_write(self, scope: str) -> bool:
        return scope in self.write_scopes

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


class AgentMessage(AriadneModel):
    """A typed handoff between two agents.

    Agents do not exchange free-form natural language. They exchange one of these, carrying
    a validated domain object, and the router checks that the receiver's manifest declares
    both the capability and the payload schema before delivery.
    """

    message_id: str = Field(min_length=1)
    from_agent: str = Field(min_length=1)
    to_agent: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    payload_schema: str = Field(min_length=1)
    payload: SerializeAsAny[AriadneModel]
    investigation_id: str
    trace_id: str | None = None
    created_at: datetime
    payload_hash: str

    @model_validator(mode="after")
    def _payload_matches_declared_schema(self) -> AgentMessage:
        actual = type(self.payload).__name__
        if actual != self.payload_schema:
            raise ValueError(
                f"message declares payload_schema={self.payload_schema!r} but carries "
                f"a {actual}"
            )
        if self.from_agent == self.to_agent:
            raise ValueError("an agent cannot hand off to itself")
        return self


class AgentInvocation(AriadneModel):
    """The audit record of one agent run.

    Written whether the run succeeded, was retried, timed out, or was quarantined. An agent
    that fails silently is indistinguishable from one that was never called, so nothing is
    allowed to fail silently.
    """

    invocation_id: str
    agent_id: str
    agent_version: str
    role: AgentRole
    investigation_id: str
    capability: str
    attempt: int = Field(ge=1, le=10)
    succeeded: bool
    input_hash: str
    output_hash: str | None = None
    error_code: str | None = None
    error_detail: str | None = Field(default=None, max_length=1000)
    latency_ms: float = Field(ge=0.0)
    llm_model: str | None = None
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)
    started_at: datetime
    trace_id: str | None = None

    @model_validator(mode="after")
    def _failure_is_explained(self) -> AgentInvocation:
        if not self.succeeded and not self.error_code:
            raise ValueError("a failed invocation must record an error_code")
        return self


class ToolCall(AriadneModel):
    """A single tool use, recorded for the permission audit trail."""

    agent_id: str
    tool: str
    investigation_id: str
    risk: RiskLevel
    allowed: bool
    denied_reason: str | None = None
    arguments_hash: str
    called_at: datetime

    @model_validator(mode="after")
    def _denial_is_explained(self) -> ToolCall:
        if not self.allowed and not self.denied_reason:
            raise ValueError("a denied tool call must record why")
        return self


def describe_manifest(manifest: AgentManifest) -> dict[str, Any]:
    """Flatten a manifest for logging and for the console's fleet view."""
    return {
        "agent_id": manifest.agent_id,
        "version": manifest.version,
        "role": str(manifest.role),
        "capabilities": list(manifest.capabilities),
        "read_scopes": list(manifest.read_scopes),
        "write_scopes": list(manifest.write_scopes),
        "tools": [t.tool for t in manifest.allowed_tools],
        "max_risk_level": str(manifest.max_risk_level),
        "uses_llm": manifest.uses_llm,
        "healthy": manifest.healthy,
    }
