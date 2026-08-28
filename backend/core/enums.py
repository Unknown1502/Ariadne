"""Closed vocabularies.

Every value an agent may emit is drawn from a closed set. An LLM cannot invent a new
verdict, action, or state, because these enums are enforced at the schema boundary.
"""

from __future__ import annotations

from enum import StrEnum


class VerdictStatus(StrEnum):
    """The only three verdicts Ariadne may reach. There is no fourth."""

    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class InvestigationState(StrEnum):
    """States in the investigation state machine (docs/11-state-machine.md)."""

    CREATED = "CREATED"
    INGESTING = "INGESTING"
    CLAIM_EXTRACTED = "CLAIM_EXTRACTED"
    PROBE_PLANNED = "PROBE_PLANNED"
    INTERVENTION_VALIDATED = "INTERVENTION_VALIDATED"
    EXPERIMENT_RUNNING = "EXPERIMENT_RUNNING"
    VERIFICATION = "VERIFICATION"
    LINEAGE_UPDATED = "LINEAGE_UPDATED"
    DEBT_RECALCULATED = "DEBT_RECALCULATED"
    GOVERNOR_ACTION = "GOVERNOR_ACTION"
    REVIEW = "REVIEW"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


class ExpectedDirection(StrEnum):
    """The behavioral change a claim predicts under its intervention."""

    INCREASE = "increase"
    DECREASE = "decrease"
    CHANGE = "change"
    NO_CHANGE = "no_change"


class InterventionType(StrEnum):
    """How a target variable is perturbed."""

    NEUTRALIZE = "neutralize"
    INCREASE = "increase"
    DECREASE = "decrease"
    SUBSTITUTE = "substitute"
    ABLATION = "ablation"


class GovernorAction(StrEnum):
    """The complete set of actions the Governor may take. Nothing else is executable."""

    NO_ACTION = "NO_ACTION"
    STORE_EVIDENCE = "STORE_EVIDENCE"
    SCHEDULE_REAUDIT = "SCHEDULE_REAUDIT"
    INCREASE_AUDIT_PRIORITY = "INCREASE_AUDIT_PRIORITY"
    MARK_EXPLANATION_STALE = "MARK_EXPLANATION_STALE"
    REQUIRE_HUMAN_REVIEW = "REQUIRE_HUMAN_REVIEW"
    PAUSE_AFFECTED_WORKFLOW = "PAUSE_AFFECTED_WORKFLOW"


class EventType(StrEnum):
    """Typed events on the Ariadne bus."""

    MODEL_REGISTERED = "MODEL_REGISTERED"
    MODEL_VERSION_DEPLOYED = "MODEL_VERSION_DEPLOYED"
    DISTRIBUTION_CHANGED = "DISTRIBUTION_CHANGED"
    EXPLANATION_RECEIVED = "EXPLANATION_RECEIVED"
    CLAIM_EXTRACTION_FAILED = "CLAIM_EXTRACTION_FAILED"
    EXPERIMENT_REQUESTED = "EXPERIMENT_REQUESTED"
    EXPERIMENT_REJECTED = "EXPERIMENT_REJECTED"
    EXPERIMENT_FAILED = "EXPERIMENT_FAILED"
    EXPERIMENT_COMPLETED = "EXPERIMENT_COMPLETED"
    VERDICT_CREATED = "VERDICT_CREATED"
    LINEAGE_UPDATED = "LINEAGE_UPDATED"
    EVIDENCE_EXPIRED = "EVIDENCE_EXPIRED"
    DEBT_UPDATED = "DEBT_UPDATED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    AUDIT_SCHEDULED = "AUDIT_SCHEDULED"
    AGENT_QUARANTINED = "AGENT_QUARANTINED"


class LineageRelation(StrEnum):
    """How a new lineage entry relates to the prior entry for the same claim family.

    Append-only means the prior row is never edited; the relation records what changed.
    """

    INITIAL = "INITIAL"
    CONFIRMS = "CONFIRMS"
    SUPERSEDES = "SUPERSEDES"
    DISPUTES = "DISPUTES"
    EXPIRES = "EXPIRES"


class RiskLevel(StrEnum):
    """Maximum blast radius an agent is permitted to reach."""

    READ_ONLY = "READ_ONLY"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class AgentRole(StrEnum):
    """The four cognitive roles. Adding a fifth requires a design change, not a config change."""

    INVESTIGATOR = "INVESTIGATOR"
    EXPERIMENTER = "EXPERIMENTER"
    VERIFIER = "VERIFIER"
    GOVERNOR = "GOVERNOR"


class RunKind(StrEnum):
    """Which arm of the experiment a run belongs to."""

    BASELINE = "BASELINE"
    INTERVENTION = "INTERVENTION"
    CONTROL = "CONTROL"
