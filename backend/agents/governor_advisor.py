"""The Governor's semantic advisor.

Wraps a language model behind the ``GovernorAdvisor`` protocol so it can recommend an
action from verified context. It is the reasoning half of the Governor role; the deciding
half lives in ``backend/governance/governor.py`` and does not import this module.

The dependency direction is the design. Governance code has no knowledge of this file, so
there is no path by which an advisor could widen its own authority - it can only return one
of the seven allowed actions, and even that is re-checked before being recorded.

Everything the advisor is shown has already been computed deterministically: the verdict,
the debt breakdown, the contradiction count, the version history. It reasons over verified
facts, never over raw model output.
"""

from __future__ import annotations

from typing import Any

from backend.agents.audit import AuditSink
from backend.agents.base import AgentBase
from backend.agents.llm import LLMClient, LLMRequest
from backend.agents.prompts import POLICY_ADVISOR_SYSTEM, build_policy_prompt
from backend.agents.registry import GOVERNOR_MANIFEST
from backend.core.agent_contracts import AgentManifest
from backend.core.clock import Clock
from backend.core.enums import GovernorAction, VerdictStatus
from backend.core.schemas import AgentProvenance
from backend.governance.governor import GovernanceContext

VALID_ACTIONS = {action.value for action in GovernorAction}


class GovernorAdvisorAgent(AgentBase):
    """Recommends an action. Never decides one."""

    def __init__(
        self,
        llm: LLMClient,
        *,
        manifest: AgentManifest = GOVERNOR_MANIFEST,
        clock: Clock | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        super().__init__(manifest, llm, clock=clock, audit=audit)

    def recommend(
        self, context: GovernanceContext
    ) -> tuple[GovernorAction | None, AgentProvenance | None]:
        """Return a recommended action, or (None, provenance) if none can be obtained.

        Failures are swallowed into None rather than raised. A missing recommendation must
        never block governance: deterministic policy has already produced a decision, and
        the recommendation is commentary on it.
        """
        request = LLMRequest(
            system=POLICY_ADVISOR_SYSTEM,
            user=build_policy_prompt(
                verdict_status=str(context.verdict.status) if context.verdict else None,
                debt_total=context.debt.total,
                debt_breakdown=[
                    {"name": c.name, "points": c.points, "detail": c.detail}
                    for c in context.debt.components
                    if c.points > 0
                ],
                contradiction_count=context.lineage.count(VerdictStatus.CONTRADICTED),
                has_expired_evidence=context.lineage.has_expired_evidence,
                evidence_is_stale=context.evidence_is_stale,
                statuses_by_version={
                    version: str(status)
                    for version, status in context.lineage.statuses_by_version.items()
                },
                audit_priority=context.audit_priority,
            ),
            task="recommend_action",
            context={
                "debt_total": context.debt.total,
                "verdict_status": str(context.verdict.status) if context.verdict else "",
                "contradiction_count": context.lineage.count(VerdictStatus.CONTRADICTED),
            },
            temperature=0.0,
        )

        try:
            _, outcome = self.reason(
                request,
                investigation_id=context.investigation_id,
                validate=self._validate,
            )
        except Exception:
            return None, None

        action = GovernorAction(outcome.payload["recommended_action"])
        return action, outcome.provenance

    @staticmethod
    def _validate(payload: dict[str, Any]) -> dict[str, Any]:
        """Accept only an action that exists.

        An invented action is a retryable malformed response, not something to translate
        into "the nearest thing we do support" - guessing at intent is how an unauthorized
        action gets executed.
        """
        if "recommended_action" not in payload:
            raise ValueError("advisor output is missing recommended_action")
        action = str(payload["recommended_action"]).strip().upper()
        if action not in VALID_ACTIONS:
            raise ValueError(
                f"recommended_action must be one of {sorted(VALID_ACTIONS)}, got {action!r}"
            )
        payload["recommended_action"] = action
        payload["rationale"] = str(payload.get("rationale", ""))[:1000]
        return payload
