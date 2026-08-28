"""The investigation pipeline.

Drives one investigation from an inbound explanation to a governance action:

    INGESTING -> CLAIM_EXTRACTED -> PROBE_PLANNED -> INTERVENTION_VALIDATED
              -> EXPERIMENT_RUNNING -> VERIFICATION -> LINEAGE_UPDATED
              -> DEBT_RECALCULATED -> GOVERNOR_ACTION -> COMPLETE

Two properties are load-bearing.

**Every step is checkpointed.** The investigation record is written to runtime state after
each transition, with the ID of whatever that step produced. A worker that dies mid-pipeline
is resumed by ``resume()``, which reads the checkpoint and continues from there instead of
starting over.

Each step therefore skips on *two* conditions: the state says it is already past, or the
artifact it would produce is already recorded on the investigation. The second check is the
one that matters, because a rolled-back state with a durable artifact still present is
exactly what a crash leaves behind. Re-deriving the artifact instead would rewrite history -
the debt snapshot, for instance, folds in the previous total, so recomputing it after it was
stored produces different content under the same ID, which the append-only ledger correctly
refuses.

**Every transition goes through the state machine.** There is no path that writes a state
directly, so "reached a verdict without running an experiment" is not a bug that could
survive here - it raises.

The pipeline is synchronous and knows nothing about Pub/Sub. The worker owns delivery,
idempotency, and retries; this owns the science.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agents.experimenter import Experimenter
from backend.agents.investigator import Investigator
from backend.agents.registry import AgentRegistry
from backend.core.clock import Clock, SystemClock
from backend.core.enums import InvestigationState as S
from backend.core.enums import VerdictStatus
from backend.core.errors import (
    AriadneError,
    InterventionRejected,
    LoopBudgetExceeded,
    UntestableExplanation,
    ValidationError,
)
from backend.core.ids import INVESTIGATION_PREFIX, derive_id
from backend.core.schemas import Investigation, VersionScope
from backend.core.state_machine import assert_transition, has_completed
from backend.debt.calculator import DebtCalculator
from backend.governance.governor import (
    GovernanceContext,
    Governor,
    build_approval_request,
)
from backend.lineage.service import LineageService
from backend.storage.runtime import RuntimeStateStore, ScheduledAudit
from backend.storage.sql import EvidenceLedger
from backend.verifier.verifier import generate_verdict


@dataclass(frozen=True, slots=True)
class InvestigationRequest:
    """What starts an investigation."""

    scope: VersionScope
    explanation: str
    decision: str
    trigger_event_id: str
    trigger_event_type: str
    priority: float = 0.5
    investigation_id: str | None = None

    def derived_id(self) -> str:
        """Content-addressed investigation ID.

        Derived from the scope and the explanation rather than randomly, so a redelivered
        event resolves to the *same* investigation and resumes it instead of starting a
        parallel one.
        """
        return self.investigation_id or derive_id(
            INVESTIGATION_PREFIX,
            self.scope.model_id,
            self.scope.model_version,
            self.scope.distribution_version,
            self.explanation,
            self.decision,
        )


@dataclass(frozen=True, slots=True)
class PipelineResult:
    investigation: Investigation
    verdict_status: VerdictStatus | None
    debt_total: float | None
    action: str | None
    resumed_from: S | None = None


class InvestigationPipeline:
    """Runs an investigation, one checkpointed step at a time."""

    def __init__(
        self,
        *,
        ledger: EvidenceLedger,
        runtime: RuntimeStateStore,
        lineage: LineageService,
        debt: DebtCalculator,
        governor: Governor,
        investigator: Investigator,
        experimenter: Experimenter,
        registry: AgentRegistry | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._ledger = ledger
        self._runtime = runtime
        self._lineage = lineage
        self._debt = debt
        self._governor = governor
        self._investigator = investigator
        self._experimenter = experimenter
        self._registry = registry or AgentRegistry.with_defaults()
        self._clock = clock or SystemClock()

    # -- entry points ------------------------------------------------------------------

    def run(self, request: InvestigationRequest) -> PipelineResult:
        """Start or resume an investigation for this request."""
        investigation_id = request.derived_id()
        existing = self._runtime.get_investigation(investigation_id)
        if existing is not None:
            return self._drive(existing, request, resumed_from=existing.state)

        now = self._clock.now()
        investigation = Investigation(
            id=investigation_id,
            scope=request.scope,
            state=S.CREATED,
            trigger_event_id=request.trigger_event_id,
            trigger_event_type=request.trigger_event_type,
            priority=request.priority,
            source_decision=request.decision,
            source_explanation=request.explanation[:4000],
            created_at=now,
            updated_at=now,
        )
        self._save(investigation)
        return self._drive(investigation, request, resumed_from=None)

    def resume(self, investigation_id: str) -> PipelineResult:
        """Continue an investigation that a previous worker left unfinished."""
        investigation = self._runtime.get_investigation(investigation_id)
        if investigation is None:
            raise ValidationError(f"no checkpoint exists for investigation {investigation_id!r}")
        if investigation.source_explanation is None:
            raise ValidationError(
                f"investigation {investigation_id!r} has no recorded explanation to resume from"
            )
        request = InvestigationRequest(
            scope=investigation.scope,
            explanation=investigation.source_explanation,
            decision=investigation.source_decision or "UNKNOWN",
            trigger_event_id=investigation.trigger_event_id,
            trigger_event_type=investigation.trigger_event_type,
            priority=investigation.priority,
            investigation_id=investigation.id,
        )
        return self._drive(investigation, request, resumed_from=investigation.state)

    # -- the pipeline ------------------------------------------------------------------

    def _drive(
        self,
        investigation: Investigation,
        request: InvestigationRequest,
        *,
        resumed_from: S | None,
    ) -> PipelineResult:
        if investigation.state in (S.COMPLETE, S.REVIEW):
            return self._result(investigation, resumed_from)
        if investigation.state in (S.FAILED, S.QUARANTINED):
            return self._result(investigation, resumed_from)

        try:
            investigation = self._ingest(investigation, request)
            investigation = self._extract_claim(investigation, request)
            investigation = self._plan_probe(investigation)
            investigation = self._validate_and_execute(investigation)
            investigation = self._verify(investigation)
            investigation = self._update_lineage(investigation)
            investigation = self._recalculate_debt(investigation)
            investigation = self._govern(investigation)
        except UntestableExplanation as exc:
            # Not a failure of the system: a finding about the explanation. Recorded as
            # CLAIM_EXTRACTION_FAILED so it is visible and countable, rather than producing
            # a verdict about a hypothesis nobody actually stated.
            investigation = self._fail(investigation, "CLAIM_EXTRACTION_FAILED", str(exc))
        except LoopBudgetExceeded as exc:
            investigation = self._quarantine(investigation, "AGENT_LOOP_BUDGET", str(exc))
        except (InterventionRejected, ValidationError) as exc:
            investigation = self._fail(investigation, type(exc).__name__, str(exc))
        except AriadneError as exc:
            if getattr(exc, "retryable", False):
                # Let the worker's retry policy handle it; keep the checkpoint intact so
                # the retry resumes rather than restarts.
                self._save(
                    investigation.model_copy(
                        update={
                            "attempts": investigation.attempts + 1,
                            "last_error": str(exc)[:1000],
                            "updated_at": self._clock.now(),
                        }
                    )
                )
                raise
            investigation = self._fail(investigation, type(exc).__name__, str(exc))

        return self._result(investigation, resumed_from)

    def _ingest(
        self, investigation: Investigation, request: InvestigationRequest
    ) -> Investigation:
        if has_completed(investigation.state, S.INGESTING):
            return investigation
        return self._advance(investigation, S.INGESTING)

    def _extract_claim(
        self, investigation: Investigation, request: InvestigationRequest
    ) -> Investigation:
        if has_completed(investigation.state, S.CLAIM_EXTRACTED):
            return investigation
        if investigation.claim_id:
            return self._advance(investigation, S.CLAIM_EXTRACTED)

        # Routing is a real check, not a diagram: the registry must confirm this agent
        # declares the capability *and* accepts this payload schema before it is called.
        self._registry.route("compile_claim", "ExplanationReceivedPayload")
        claim, _ = self._investigator.compile_claim(
            explanation=request.explanation,
            decision=request.decision,
            scope=request.scope,
            investigation_id=investigation.id,
            valid_from=self._clock.now(),
        )
        self._ledger.append_claim(claim)

        if claim.quarantined:
            # A poisoned explanation is recorded, then stopped. The attempt is evidence.
            return self._quarantine(
                investigation.model_copy(
                    update={"claim_id": claim.id, "claim_family_id": claim.claim_family_id}
                ),
                "QUARANTINED_INPUT",
                f"explanation flagged: {claim.quarantine_reasons}",
            )

        return self._advance(
            investigation,
            S.CLAIM_EXTRACTED,
            claim_id=claim.id,
            claim_family_id=claim.claim_family_id,
        )

    def _plan_probe(self, investigation: Investigation) -> Investigation:
        if has_completed(investigation.state, S.PROBE_PLANNED):
            return investigation
        if investigation.experiment_id:
            return self._advance(investigation, S.PROBE_PLANNED)
        self._registry.route("plan_experiment", "Claim")
        claim = self._require_claim(investigation)
        plan, _ = self._experimenter.plan_experiment(claim, created_at=self._clock.now())
        self._ledger.append_plan(plan)
        return self._advance(investigation, S.PROBE_PLANNED, experiment_id=plan.id)

    def _validate_and_execute(self, investigation: Investigation) -> Investigation:
        if has_completed(investigation.state, S.EXPERIMENT_RUNNING):
            return investigation
        if investigation.evidence_id:
            if not has_completed(investigation.state, S.INTERVENTION_VALIDATED):
                investigation = self._advance(investigation, S.INTERVENTION_VALIDATED)
            return self._advance(investigation, S.EXPERIMENT_RUNNING)

        claim = self._require_claim(investigation)
        plan = self._require_plan(investigation)

        if not has_completed(investigation.state, S.INTERVENTION_VALIDATED):
            investigation = self._advance(investigation, S.INTERVENTION_VALIDATED)

        investigation = self._advance(investigation, S.EXPERIMENT_RUNNING)
        result = self._experimenter.execute(plan, claim)

        for run in result.runs:
            self._ledger.append_run(run)
        self._ledger.append_evidence(result.evidence)
        return self._save(
            investigation.model_copy(
                update={"evidence_id": result.evidence.id, "updated_at": self._clock.now()}
            )
        )

    def _verify(self, investigation: Investigation) -> Investigation:
        if has_completed(investigation.state, S.VERIFICATION):
            return investigation
        if investigation.verdict_id:
            return self._advance(investigation, S.VERIFICATION)

        claim = self._require_claim(investigation)
        plan = self._require_plan(investigation)
        evidence = self._ledger.get_evidence(investigation.evidence_id or "")
        if evidence is None:
            raise ValidationError(
                f"investigation {investigation.id} reached verification with no evidence"
            )

        investigation = self._advance(investigation, S.VERIFICATION)
        verdict = generate_verdict(evidence, claim, plan, created_at=self._clock.now())
        self._ledger.append_verdict(verdict)
        return self._save(
            investigation.model_copy(
                update={"verdict_id": verdict.id, "updated_at": self._clock.now()}
            )
        )

    def _update_lineage(self, investigation: Investigation) -> Investigation:
        if has_completed(investigation.state, S.LINEAGE_UPDATED):
            return investigation
        if investigation.lineage_entry_id:
            return self._advance(investigation, S.LINEAGE_UPDATED)
        verdict = self._ledger.get_verdict(investigation.verdict_id or "")
        evidence = self._ledger.get_evidence(investigation.evidence_id or "")
        if verdict is None or evidence is None:
            raise ValidationError(
                f"investigation {investigation.id} cannot update lineage without both a "
                f"verdict and its evidence"
            )
        entry = self._lineage.append_verdict(verdict, evidence)
        return self._advance(investigation, S.LINEAGE_UPDATED, lineage_entry_id=entry.id)

    def _recalculate_debt(self, investigation: Investigation) -> Investigation:
        if has_completed(investigation.state, S.DEBT_RECALCULATED):
            return investigation
        if investigation.debt_snapshot_id:
            return self._advance(investigation, S.DEBT_RECALCULATED)
        previous = self._ledger.latest_debt(investigation.scope.model_id)
        snapshot = self._debt.calculate(
            investigation.scope.model_id,
            scope_label=investigation.scope.label(),
            previous_total=previous.total if previous else None,
            trigger_event_id=investigation.trigger_event_id,
            current_distribution=investigation.scope.distribution_version,
        )
        self._ledger.append_debt(snapshot)
        return self._advance(
            investigation, S.DEBT_RECALCULATED, debt_snapshot_id=snapshot.id
        )

    def _govern(self, investigation: Investigation) -> Investigation:
        if has_completed(investigation.state, S.GOVERNOR_ACTION):
            return investigation
        if investigation.decision_id:
            decision = self._ledger.get_decision(investigation.decision_id)
            investigation = self._advance(investigation, S.GOVERNOR_ACTION)
            return self._advance(
                investigation,
                S.REVIEW if decision and decision.required_approval else S.COMPLETE,
            )

        verdict = self._ledger.get_verdict(investigation.verdict_id or "")
        snapshot = self._ledger.get_debt_snapshot(investigation.debt_snapshot_id or "")
        family = investigation.claim_family_id or ""
        if snapshot is None:
            raise ValidationError("governance requires a debt snapshot")

        view = self._lineage.view(family)
        current = view.current
        context = GovernanceContext(
            verdict=verdict,
            lineage=view,
            debt=snapshot,
            audit_priority=self._lineage.audit_priority(family),
            evidence_is_stale=bool(current and self._lineage.is_stale(current)),
            scope=investigation.scope,
            investigation_id=investigation.id,
        )
        decision = self._governor.govern(context)
        self._ledger.append_decision(decision)

        investigation = self._advance(
            investigation, S.GOVERNOR_ACTION, decision_id=decision.id
        )

        if decision.next_event_at is not None:
            self._runtime.schedule_audit(
                ScheduledAudit(
                    id=derive_id("AUD", decision.id),
                    claim_family_id=family,
                    model_id=investigation.scope.model_id,
                    scheduled_for=decision.next_event_at,
                    priority=context.audit_priority,
                    reason_code=decision.reason_codes[0],
                    created_at=self._clock.now(),
                )
            )

        if decision.required_approval:
            # The action is NOT carried out. It waits for a person.
            self._runtime.save_approval(
                build_approval_request(decision, requested_at=self._clock.now())
            )
            return self._advance(investigation, S.REVIEW)

        return self._advance(investigation, S.COMPLETE)

    # -- helpers -----------------------------------------------------------------------

    def _advance(self, investigation: Investigation, target: S, **fields: Any) -> Investigation:
        assert_transition(investigation.state, target)
        updated = investigation.with_state(target, self._clock.now(), **fields)
        return self._save(updated)

    def _save(self, investigation: Investigation) -> Investigation:
        self._runtime.save_investigation(investigation)
        return investigation

    def _fail(self, investigation: Investigation, code: str, detail: str) -> Investigation:
        return self._save(
            investigation.with_state(
                S.FAILED, self._clock.now(), last_error=f"{code}: {detail}"[:1000]
            )
        )

    def _quarantine(
        self, investigation: Investigation, code: str, detail: str
    ) -> Investigation:
        return self._save(
            investigation.with_state(
                S.QUARANTINED, self._clock.now(), last_error=f"{code}: {detail}"[:1000]
            )
        )

    def _require_claim(self, investigation: Investigation):
        claim = self._ledger.get_claim(investigation.claim_id or "")
        if claim is None:
            raise ValidationError(
                f"investigation {investigation.id} has no recorded claim to work from"
            )
        return claim

    def _require_plan(self, investigation: Investigation):
        plan = self._ledger.get_plan(investigation.experiment_id or "")
        if plan is None:
            raise ValidationError(
                f"investigation {investigation.id} has no recorded experiment plan"
            )
        return plan

    def _result(
        self, investigation: Investigation, resumed_from: S | None
    ) -> PipelineResult:
        verdict = self._ledger.get_verdict(investigation.verdict_id or "")
        snapshot = self._ledger.get_debt_snapshot(investigation.debt_snapshot_id or "")
        decision = self._ledger.get_decision(investigation.decision_id or "")
        return PipelineResult(
            investigation=investigation,
            verdict_status=verdict.status if verdict else None,
            debt_total=snapshot.total if snapshot else None,
            action=str(decision.action) if decision else None,
            resumed_from=resumed_from,
        )


def build_pipeline(
    *,
    ledger: EvidenceLedger,
    runtime: RuntimeStateStore,
    clock: Clock,
    llm=None,
    policy=None,
    default_repetitions: int = 24,
    default_seed: int = 20260101,
    evidence_validity_days: int | None = None,
    agent_loop_budget: int | None = None,
    agent_timeout_seconds: float | None = None,
) -> InvestigationPipeline:
    """Assemble a pipeline with the standard wiring."""
    from backend.agents.audit import LedgerAuditSink
    from backend.agents.governor_advisor import GovernorAdvisorAgent
    from backend.agents.llm import build_llm_client
    from backend.agents.registry import (
        EXPERIMENTER_MANIFEST,
        GOVERNOR_MANIFEST,
        INVESTIGATOR_MANIFEST,
        apply_limits,
    )
    from backend.config import get_settings
    from backend.experiment_engine.runner import ExperimentRunner
    from backend.governance.policy import DEFAULT_POLICY

    settings = get_settings()
    reasoner = llm or build_llm_client()
    active_policy = policy or DEFAULT_POLICY
    lineage = LineageService(
        ledger,
        clock=clock,
        validity_days=evidence_validity_days or settings.evidence_validity_days,
    )

    def bounded(manifest):
        return apply_limits(
            manifest,
            loop_budget=agent_loop_budget or settings.agent_loop_budget,
            timeout_seconds=agent_timeout_seconds or settings.agent_timeout_seconds,
        )
    # Agent invocations and tool-permission decisions land in the ledger's audit_events
    # table. Without this the table stays permanently empty while the docstrings claim
    # every attempt is recorded.
    audit = LedgerAuditSink(ledger)

    return InvestigationPipeline(
        ledger=ledger,
        runtime=runtime,
        lineage=lineage,
        debt=DebtCalculator(lineage, policy=active_policy, clock=clock),
        governor=Governor(
            policy=active_policy,
            advisor=GovernorAdvisorAgent(
                reasoner, manifest=bounded(GOVERNOR_MANIFEST), clock=clock, audit=audit
            ),
            clock=clock,
        ),
        investigator=Investigator(
            reasoner,
            manifest=bounded(INVESTIGATOR_MANIFEST),
            lineage=lineage,
            clock=clock,
            audit=audit,
        ),
        experimenter=Experimenter(
            reasoner,
            manifest=bounded(EXPERIMENTER_MANIFEST),
            runner=ExperimentRunner(clock=clock, run_store=runtime),
            clock=clock,
            audit=audit,
            default_repetitions=default_repetitions,
            default_seed=default_seed,
        ),
        clock=clock,
    )
