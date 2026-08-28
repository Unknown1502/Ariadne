"""The agent registry (prompt 10).

Four roles, four manifests, four different sets of authority. The registry is what turns
"we have four agents" from an architecture diagram into an enforced boundary: a typed
message is routed only to an agent whose manifest declares both the capability and the
payload schema, and an agent that repeatedly misbehaves is quarantined out of the fleet.

Read the write scopes below as the design's actual claim:

    investigator  writes claims.                      Not evidence. Not verdicts.
    experimenter  writes plans, runs, evidence.       Not verdicts.
    verifier      writes verdicts and lineage.        Uses no LLM at all.
    governor      writes decisions and schedules.     Not evidence. Not verdicts.

Those absences are the security model. A compromised Investigator - via a poisoned
explanation, say - cannot write a verdict, because no code path grants it one to write.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.core.agent_contracts import AgentManifest, ToolPermission
from backend.core.enums import AgentRole, RiskLevel
from backend.core.errors import PermissionDenied

INVESTIGATOR_MANIFEST = AgentManifest(
    agent_id="investigator",
    version="1.0.0",
    role=AgentRole.INVESTIGATOR,
    owner="ariadne-core",
    description="Compiles a model explanation into an executable behavioral claim.",
    capabilities=["compile_claim"],
    input_schema="ExplanationReceivedPayload",
    output_schema="Claim",
    read_scopes=["lineage.read", "policy.read", "claim.read"],
    write_scopes=["claim"],
    allowed_tools=[
        ToolPermission(tool="lineage.get_family", max_risk=RiskLevel.READ_ONLY),
        ToolPermission(tool="lineage.audit_priority", max_risk=RiskLevel.READ_ONLY),
    ],
    max_risk_level=RiskLevel.LOW,
    loop_budget=3,
    timeout_seconds=30.0,
    service_account="ariadne-investigator@PROJECT.iam.gserviceaccount.com",
    prompt_version="claim-compiler/1.0.0",
    uses_llm=True,
)

EXPERIMENTER_MANIFEST = AgentManifest(
    agent_id="experimenter",
    version="1.0.0",
    role=AgentRole.EXPERIMENTER,
    owner="ariadne-core",
    description="Designs a constrained probe and executes it against the target model.",
    capabilities=["plan_experiment", "execute_experiment"],
    input_schema="Claim",
    output_schema="Evidence",
    read_scopes=["claim.read", "policy.read"],
    write_scopes=["experiment", "experiment_run", "evidence.raw"],
    allowed_tools=[
        ToolPermission(tool="target_model.predict", max_risk=RiskLevel.LOW,
                       rate_limit_per_investigation=500),
        ToolPermission(tool="fixtures.load", max_risk=RiskLevel.READ_ONLY),
    ],
    max_risk_level=RiskLevel.MEDIUM,
    loop_budget=3,
    timeout_seconds=60.0,
    service_account="ariadne-experimenter@PROJECT.iam.gserviceaccount.com",
    prompt_version="probe-designer/1.0.0",
    uses_llm=True,
)

VERIFIER_MANIFEST = AgentManifest(
    agent_id="verifier",
    version="1.0.0",
    role=AgentRole.VERIFIER,
    owner="ariadne-core",
    description="Deterministic service. Computes the verdict from evidence. No LLM.",
    capabilities=["verify_evidence"],
    input_schema="Evidence",
    output_schema="Verdict",
    read_scopes=["evidence.read", "claim.read", "experiment.read"],
    write_scopes=["verdict", "lineage"],
    allowed_tools=[],
    max_risk_level=RiskLevel.READ_ONLY,
    loop_budget=1,
    timeout_seconds=15.0,
    service_account="ariadne-verifier@PROJECT.iam.gserviceaccount.com",
    prompt_version=None,
    uses_llm=False,
)

GOVERNOR_MANIFEST = AgentManifest(
    agent_id="governor",
    version="1.0.0",
    role=AgentRole.GOVERNOR,
    owner="ariadne-core",
    description="Turns verified evidence and debt into a bounded policy action.",
    capabilities=["recommend_action", "govern"],
    input_schema="GovernanceContext",
    output_schema="GovernorDecision",
    read_scopes=["verdict.read", "lineage.read", "debt.read", "policy.read"],
    write_scopes=["decision", "scheduled_audit", "approval_request"],
    allowed_tools=[
        ToolPermission(tool="debt.read", max_risk=RiskLevel.READ_ONLY),
        ToolPermission(tool="schedule.create", max_risk=RiskLevel.MEDIUM),
        ToolPermission(tool="approval.request", max_risk=RiskLevel.MEDIUM),
    ],
    max_risk_level=RiskLevel.MEDIUM,
    loop_budget=2,
    timeout_seconds=30.0,
    service_account="ariadne-governor@PROJECT.iam.gserviceaccount.com",
    prompt_version="policy-advisor/1.0.0",
    uses_llm=True,
)

DEFAULT_MANIFESTS: tuple[AgentManifest, ...] = (
    INVESTIGATOR_MANIFEST,
    EXPERIMENTER_MANIFEST,
    VERIFIER_MANIFEST,
    GOVERNOR_MANIFEST,
)


@dataclass
class QuarantineRecord:
    agent_id: str
    reason_code: str
    detail: str
    failures: int = 0


@dataclass
class AgentRegistry:
    """Holds manifests, routes typed messages, and quarantines failing agents."""

    manifests: dict[str, AgentManifest] = field(default_factory=dict)
    quarantined: dict[str, QuarantineRecord] = field(default_factory=dict)
    failure_threshold: int = 3

    @classmethod
    def with_defaults(cls) -> AgentRegistry:
        registry = cls()
        for manifest in DEFAULT_MANIFESTS:
            registry.register(manifest)
        return registry

    def register(self, manifest: AgentManifest) -> None:
        existing = self.manifests.get(manifest.agent_id)
        if existing is not None and existing.version != manifest.version:
            # Replacing a manifest with a different version is a deployment, and it must be
            # visible rather than silent.
            self.quarantined.pop(manifest.agent_id, None)
        self.manifests[manifest.agent_id] = manifest

    def get(self, agent_id: str) -> AgentManifest:
        try:
            return self.manifests[agent_id]
        except KeyError:
            raise PermissionDenied(f"no agent registered as {agent_id!r}") from None

    def route(self, capability: str, payload_schema: str) -> AgentManifest:
        """Find the agent that may handle this capability with this payload type.

        Both must match. Capability alone is not enough: an agent that handles
        `plan_experiment` for a Claim must not receive a `plan_experiment` carrying
        something else, because its prompt and validation assume the declared shape.
        """
        candidates = [
            manifest
            for manifest in self.manifests.values()
            if manifest.supports(capability)
            and manifest.input_schema == payload_schema
            and manifest.healthy
            and manifest.agent_id not in self.quarantined
        ]
        if not candidates:
            reason = self._routing_failure(capability, payload_schema)
            raise PermissionDenied(reason)
        return candidates[0]

    def _routing_failure(self, capability: str, payload_schema: str) -> str:
        by_capability = [m for m in self.manifests.values() if m.supports(capability)]
        if not by_capability:
            return f"no registered agent declares the capability {capability!r}"
        quarantined = [m for m in by_capability if m.agent_id in self.quarantined]
        if quarantined:
            return (
                f"the agent for {capability!r} is quarantined: "
                f"{self.quarantined[quarantined[0].agent_id].reason_code}"
            )
        schemas = sorted({m.input_schema for m in by_capability})
        return (
            f"{capability!r} is declared for input schema(s) {schemas}, but the message "
            f"carries {payload_schema!r}"
        )

    def record_failure(self, agent_id: str, reason_code: str, detail: str) -> bool:
        """Count a failure and quarantine the agent once it crosses the threshold.

        Returns True when this failure caused quarantine. Bounded failure handling is what
        stops a malfunctioning agent from consuming the fleet's budget indefinitely.
        """
        failures = self.failure_count(agent_id) + 1
        if failures >= self.failure_threshold:
            self.quarantined[agent_id] = QuarantineRecord(
                agent_id=agent_id, reason_code=reason_code, detail=detail[:1000],
                failures=failures,
            )
            return True
        self.quarantined.pop(agent_id, None)
        self._pending_failures[agent_id] = failures
        return False

    def __post_init__(self) -> None:
        self._pending_failures: dict[str, int] = {}

    def failure_count(self, agent_id: str) -> int:
        record = self.quarantined.get(agent_id)
        if record:
            return record.failures
        return self._pending_failures.get(agent_id, 0)

    def quarantine(self, agent_id: str, reason_code: str, detail: str) -> QuarantineRecord:
        """Quarantine immediately, regardless of the failure count."""
        record = QuarantineRecord(
            agent_id=agent_id, reason_code=reason_code, detail=detail[:1000],
            failures=self.failure_threshold,
        )
        self.quarantined[agent_id] = record
        return record

    def release(self, agent_id: str) -> None:
        self.quarantined.pop(agent_id, None)
        self._pending_failures.pop(agent_id, None)

    def is_quarantined(self, agent_id: str) -> bool:
        return agent_id in self.quarantined

    def healthy_agents(self) -> list[AgentManifest]:
        return [
            manifest
            for manifest in self.manifests.values()
            if manifest.healthy and manifest.agent_id not in self.quarantined
        ]

    def describe(self) -> list[dict[str, object]]:
        """Fleet summary for the console."""
        from backend.core.agent_contracts import describe_manifest

        rows = []
        for manifest in self.manifests.values():
            row = describe_manifest(manifest)
            row["quarantined"] = manifest.agent_id in self.quarantined
            row["failures"] = self.failure_count(manifest.agent_id)
            rows.append(row)
        return rows


def apply_limits(
    manifest: AgentManifest, *, loop_budget: int, timeout_seconds: float
) -> AgentManifest:
    """Apply global ceilings to a manifest.

    Tightens only. Per-agent budgets are deliberate - the Verifier gets a single attempt
    because a deterministic computation that failed once will fail identically, and the
    Experimenter gets longer because it executes runs. A global override that *raised* those
    would erase the tuning, so the minimum wins.
    """
    return manifest.model_copy(
        update={
            "loop_budget": min(manifest.loop_budget, loop_budget),
            "timeout_seconds": min(manifest.timeout_seconds, timeout_seconds),
        }
    )


def assert_four_roles(registry: AgentRegistry) -> None:
    """Fail loudly if the fleet ever grows a fifth cognitive role.

    The four-role split is a design claim Ariadne makes publicly. Anything else that needs
    doing is a deterministic service, not another agent, and this check keeps that honest as
    the code grows.
    """
    roles = {manifest.role for manifest in registry.manifests.values()}
    if roles != set(AgentRole):
        raise PermissionDenied(
            f"the fleet must contain exactly the four cognitive roles; found {sorted(roles)}"
        )
    if len(registry.manifests) != len(AgentRole):
        raise PermissionDenied(
            f"expected exactly {len(AgentRole)} agents, found {len(registry.manifests)}: "
            f"{sorted(registry.manifests)}"
        )
