"""The Governor.

The Governor turns verified evidence into an operational action. It is the only role that
can cause something to happen outside Ariadne, which is why its authority is the most
tightly bounded of the four.

The division of labour, precisely:

  - **Gemini may recommend.** Interpreting "two contradictions on a claim the nurse relies
    on, debt climbing, distribution recently shifted" is genuinely a judgement call, and a
    language model is good at reading that kind of context.
  - **Deterministic code decides.** ``decide()`` below is a pure function of the verdict,
    the lineage, the debt score, and the policy. The recommendation is recorded next to the
    enforced action, along with whether policy agreed.

So the recommendation is *auditable* rather than *authoritative*. When the model suggests
NO_ACTION and policy requires human review, the decision records both - and a reviewer can
later count how often the model wanted to do less than the rules did.

What the Governor structurally cannot do: change debt weights (the policy is a frozen
dataclass it receives), delete evidence (it holds no delete API - none exists), overrule the
verifier (it reads verdicts, never writes them), or invent an action (the enum is closed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from backend.core.clock import Clock, SystemClock
from backend.core.enums import GovernorAction, VerdictStatus
from backend.core.errors import PermissionDenied
from backend.core.ids import APPROVAL_PREFIX, DECISION_PREFIX, derive_id
from backend.core.schemas import (
    AgentProvenance,
    ApprovalRequest,
    DebtSnapshot,
    GovernorDecision,
    Verdict,
    VersionScope,
)
from backend.governance.policy import DEFAULT_POLICY, Policy
from backend.lineage.service import LineageView

ALLOWED_ACTIONS: frozenset[GovernorAction] = frozenset(GovernorAction)
"""The complete executable set. An action outside it is not 'unsupported', it does not
exist - there is no code path that could carry it out."""

ACTIONS_REQUIRING_APPROVAL: frozenset[GovernorAction] = frozenset(
    {GovernorAction.PAUSE_AFFECTED_WORKFLOW, GovernorAction.REQUIRE_HUMAN_REVIEW}
)
"""High-impact actions never execute autonomously. They open an approval request and wait."""


class ReasonCode:
    HIGH_DEBT = "HIGH_DEBT"
    CRITICAL_DEBT = "CRITICAL_DEBT"
    REPEATED_CONTRADICTION = "REPEATED_CONTRADICTION"
    CURRENT_CONTRADICTION = "CURRENT_CONTRADICTION"
    INCONCLUSIVE_EVIDENCE = "INCONCLUSIVE_EVIDENCE"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    EXPIRED_EVIDENCE = "EXPIRED_EVIDENCE"
    VERSION_INCONSISTENCY = "VERSION_INCONSISTENCY"
    EVIDENCE_CURRENT = "EVIDENCE_CURRENT"
    NOTHING_OUTSTANDING = "NOTHING_OUTSTANDING"
    RECOMMENDATION_OVERRULED = "RECOMMENDATION_OVERRULED"
    RECOMMENDATION_REJECTED = "RECOMMENDATION_REJECTED"


@dataclass(frozen=True, slots=True)
class GovernanceContext:
    """Everything the decision depends on. Assembled by the caller, read-only here."""

    verdict: Verdict | None
    lineage: LineageView
    debt: DebtSnapshot
    audit_priority: float
    evidence_is_stale: bool
    scope: VersionScope
    investigation_id: str


@dataclass(frozen=True, slots=True)
class Decision:
    """The deterministic outcome, before it is packaged as a record."""

    action: GovernorAction
    reason_codes: list[str] = field(default_factory=list)
    required_approval: bool = False
    next_event_at: datetime | None = None


class GovernorAdvisor(Protocol):
    """An optional semantic advisor. May recommend; may not decide."""

    def recommend(
        self, context: GovernanceContext
    ) -> tuple[GovernorAction | None, AgentProvenance | None]: ...


def decide(
    context: GovernanceContext, policy: Policy = DEFAULT_POLICY, *, now: datetime
) -> Decision:
    """Choose the action. Pure function of context and policy.

    Rules are evaluated most-severe first and the first match wins, so the outcome is a
    single, explainable branch rather than a blend of overlapping heuristics.
    """
    thresholds = policy.thresholds
    debt = context.debt.total
    status = context.verdict.status if context.verdict else None
    contradictions = context.lineage.count(VerdictStatus.CONTRADICTED)
    reasons: list[str] = []

    # 1. Critical debt on a currently-contradicted explanation. The only action that can
    #    interrupt a workflow, and it still requires a human to confirm.
    if debt >= thresholds.pause_workflow_debt and status is VerdictStatus.CONTRADICTED:
        return Decision(
            action=GovernorAction.PAUSE_AFFECTED_WORKFLOW,
            reason_codes=[ReasonCode.CRITICAL_DEBT, ReasonCode.CURRENT_CONTRADICTION],
            required_approval=True,
        )

    # 2. High debt, or a claim that has now failed more than once.
    if debt >= thresholds.human_review_debt:
        reasons.append(ReasonCode.HIGH_DEBT)
    if contradictions >= thresholds.repeated_contradiction_count:
        reasons.append(ReasonCode.REPEATED_CONTRADICTION)
    if reasons:
        return Decision(
            action=GovernorAction.REQUIRE_HUMAN_REVIEW,
            reason_codes=reasons,
            required_approval=True,
        )

    # 3. Evidence that no longer describes the world it was measured in.
    if context.lineage.has_expired_evidence or context.evidence_is_stale:
        return Decision(
            action=GovernorAction.MARK_EXPLANATION_STALE,
            reason_codes=[
                ReasonCode.EXPIRED_EVIDENCE
                if context.lineage.has_expired_evidence
                else ReasonCode.STALE_EVIDENCE
            ],
            next_event_at=now + _reaudit_delay(context.audit_priority),
        )

    # 4. A fresh contradiction: raise this family's priority for the next sweep.
    if status is VerdictStatus.CONTRADICTED:
        codes = [ReasonCode.CURRENT_CONTRADICTION]
        if len({e.status for e in context.lineage.entries}) > 1:
            codes.append(ReasonCode.VERSION_INCONSISTENCY)
        return Decision(
            action=GovernorAction.INCREASE_AUDIT_PRIORITY,
            reason_codes=codes,
            next_event_at=now + _reaudit_delay(context.audit_priority),
        )

    # 5. An inconclusive result is unfinished work, not a conclusion.
    if status is VerdictStatus.INCONCLUSIVE:
        return Decision(
            action=GovernorAction.SCHEDULE_REAUDIT,
            reason_codes=[ReasonCode.INCONCLUSIVE_EVIDENCE],
            next_event_at=now + _reaudit_delay(context.audit_priority),
        )

    # 6. Debt above the lowest threshold still warrants a scheduled look.
    if debt >= thresholds.schedule_reaudit_debt:
        return Decision(
            action=GovernorAction.SCHEDULE_REAUDIT,
            reason_codes=[ReasonCode.HIGH_DEBT],
            next_event_at=now + _reaudit_delay(context.audit_priority),
        )

    # 7. Supported and quiet: record the evidence and move on.
    if status is VerdictStatus.SUPPORTED:
        return Decision(
            action=GovernorAction.STORE_EVIDENCE,
            reason_codes=[ReasonCode.EVIDENCE_CURRENT],
        )

    return Decision(
        action=GovernorAction.NO_ACTION, reason_codes=[ReasonCode.NOTHING_OUTSTANDING]
    )


def _reaudit_delay(priority: float) -> timedelta:
    """Higher priority, sooner re-audit. Deterministic so the schedule is testable."""
    if priority >= 0.9:
        return timedelta(hours=1)
    if priority >= 0.7:
        return timedelta(days=1)
    if priority >= 0.4:
        return timedelta(days=7)
    return timedelta(days=30)


class Governor:
    """Packages a deterministic decision, optionally after consulting an advisor."""

    agent_id = "governor"
    agent_version = "1.0.0"

    def __init__(
        self,
        *,
        policy: Policy = DEFAULT_POLICY,
        advisor: GovernorAdvisor | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._policy = policy
        self._advisor = advisor
        self._clock = clock or SystemClock()

    def govern(self, context: GovernanceContext) -> GovernorDecision:
        """Decide, record what the advisor wanted, and never let it win by default."""
        now = self._clock.now()
        enforced = decide(context, self._policy, now=now)

        recommendation: GovernorAction | None = None
        provenance: AgentProvenance | None = None
        reasons = list(enforced.reason_codes)

        if self._advisor is not None:
            recommendation, provenance = self._consult(context)
            if recommendation is not None and recommendation != enforced.action:
                reasons.append(ReasonCode.RECOMMENDATION_OVERRULED)

        return GovernorDecision(
            id=derive_id(
                DECISION_PREFIX,
                context.investigation_id,
                str(enforced.action),
                context.debt.id,
                self._policy.version,
            ),
            investigation_id=context.investigation_id,
            claim_family_id=context.lineage.claim_family_id,
            scope=context.scope,
            action=enforced.action,
            reason_codes=sorted(set(reasons)) or [ReasonCode.NOTHING_OUTSTANDING],
            rationale=self._rationale(context, enforced, recommendation),
            policy_version=self._policy.version,
            debt_snapshot_id=context.debt.id,
            debt_total=context.debt.total,
            recommendation=recommendation,
            recommendation_accepted=(
                recommendation == enforced.action if recommendation is not None else True
            ),
            recommendation_provenance=provenance,
            required_approval=enforced.action in ACTIONS_REQUIRING_APPROVAL,
            next_event_at=enforced.next_event_at,
            created_at=now,
        )

    def _consult(
        self, context: GovernanceContext
    ) -> tuple[GovernorAction | None, AgentProvenance | None]:
        """Ask the advisor, and discard anything outside the allowed set.

        A model that returns an action Ariadne cannot execute is not an error to propagate;
        it is a recommendation to ignore, recorded as ignored.
        """
        assert self._advisor is not None
        try:
            recommendation, provenance = self._advisor.recommend(context)
        except Exception:
            # An advisor failure must never block governance. Deterministic policy already
            # produced the decision; the recommendation is a nicety.
            return None, None
        if recommendation is not None and recommendation not in ALLOWED_ACTIONS:
            return None, provenance
        return recommendation, provenance

    def _rationale(
        self,
        context: GovernanceContext,
        enforced: Decision,
        recommendation: GovernorAction | None,
    ) -> str:
        parts = [
            f"{enforced.action} under policy {self._policy.version}",
            f"debt={context.debt.total:.1f}",
            f"verdict={context.verdict.status if context.verdict else 'none'}",
            f"contradictions={context.lineage.count(VerdictStatus.CONTRADICTED)}",
            f"audit_priority={context.audit_priority:.2f}",
        ]
        if recommendation is not None and recommendation != enforced.action:
            parts.append(
                f"advisor recommended {recommendation}, overruled by deterministic policy"
            )
        elif recommendation is not None:
            parts.append(f"advisor concurred ({recommendation})")
        return "; ".join(parts) + "."


def build_approval_request(
    decision: GovernorDecision, *, requested_at: datetime
) -> ApprovalRequest:
    """Open the human gate for a high-impact action.

    Returned in PENDING state: the action has *not* been carried out. Whoever executes
    actions must check for an approval and honour it.
    """
    if not decision.required_approval:
        raise PermissionDenied(
            f"{decision.action} does not require approval; opening a request for it would "
            f"misrepresent what the system is waiting on"
        )
    return ApprovalRequest(
        id=derive_id(APPROVAL_PREFIX, decision.id),
        decision_id=decision.id,
        investigation_id=decision.investigation_id,
        action=decision.action,
        justification=decision.rationale,
        status="PENDING",
        requested_at=requested_at,
    )
