"""The deterministic Verifier.

This module decides SUPPORTED, CONTRADICTED, or INCONCLUSIVE. It is a pure function of the
evidence, the claim, and the plan's declared thresholds. It contains no language model, no
network call, no clock read, and no randomness, so any reviewer can recompute a published
verdict from the stored evidence and get the same answer.

That property is the point of the whole architecture. Gemini proposes what to test; this
file decides what was found. If an LLM could touch this decision, every verdict would
inherit the LLM's failure modes, and "the model audited itself and approved" is not a
result anybody should accept.

Precedence of the rules, which matters as much as the rules themselves:

    invalid probe  ->  too few runs  ->  unstable model  ->  what the data showed

The first three all yield INCONCLUSIVE. They run first because a broken test cannot
contradict a claim - only a *working* test that fails to find the predicted effect can.
Collapsing those two situations is the fastest way to manufacture false contradictions,
and it is the mistake this ordering exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from backend.core.enums import ExpectedDirection, RunKind, VerdictStatus
from backend.core.errors import ValidationError
from backend.core.ids import verdict_id
from backend.core.schemas import (
    Claim,
    ConstraintSpec,
    Evidence,
    ExperimentPlan,
    RunSummary,
    Verdict,
)
from backend.core.versions import VERIFIER_VERSION
from backend.verifier.statistics import (
    direction_of,
    effect_size,
    paired_deltas,
    reproducibility,
    standardized_effect,
)

CONTROL_DOMINANCE_FRACTION: float = 0.5
"""A control refutes a primacy claim only when it beats the claimed driver by at least half
the minimum effect threshold. Requiring a margin rather than a bare inequality stops a
verdict from flipping on a difference too small to have been measured."""


class ReasonCode:
    """The closed vocabulary of verdict reasons.

    Verdicts are explained by composing these, never by free text from a model. A reason
    code can be counted, filtered, and regression-tested; a sentence cannot.
    """

    VALID_INTERVENTION = "VALID_INTERVENTION"
    INVALID_INTERVENTION = "INVALID_INTERVENTION"
    WEAK_PERTURBATION = "WEAK_PERTURBATION"
    CONSTRAINT_VIOLATION = "CONSTRAINT_VIOLATION"
    OUT_OF_SCOPE_MUTATION = "OUT_OF_SCOPE_MUTATION"
    INSUFFICIENT_RUNS = "INSUFFICIENT_RUNS"
    MODEL_UNSTABLE = "MODEL_UNSTABLE"
    EFFECT_REPRODUCIBLE = "EFFECT_REPRODUCIBLE"
    EFFECT_REPRODUCIBLY_ABSENT = "EFFECT_REPRODUCIBLY_ABSENT"
    EFFECT_NOT_REPRODUCIBLE = "EFFECT_NOT_REPRODUCIBLE"
    DIRECTION_MISMATCH = "DIRECTION_MISMATCH"
    CONTROL_DOMINATES = "CONTROL_DOMINATES"
    CONTROL_ABSENT = "CONTROL_ABSENT"
    PRIMACY_REFUTED = "PRIMACY_REFUTED"
    VAGUE_CLAIM = "VAGUE_CLAIM"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"


MIN_TESTABILITY: float = 0.30
"""Below this, the compiled claim is too vague for any result to mean much. The Investigator
scores its own output here, and a low score buys an INCONCLUSIVE rather than a guess."""


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """The verifier's decision plus every number it used to reach it."""

    status: VerdictStatus
    behavioral_support: float
    intervention_validity: float
    reproducibility: float
    contradiction_score: float
    effect_size: float
    standardized_effect: float
    control_effect_size: float | None
    expected_direction: ExpectedDirection
    observed_direction: ExpectedDirection
    reason_codes: list[str] = field(default_factory=list)
    rationale: str = ""


# --------------------------------------------------------------------------------------
# Individual checks (prompt 06 names these explicitly)
# --------------------------------------------------------------------------------------


def validate_constraints(evidence: Evidence, constraints: ConstraintSpec) -> bool:
    """Whether the executed experiment respected its declared constraints.

    The per-case check already ran inside the engine and is summarized in
    ``evidence.validity_score``; a zero there means a hard violation, not merely a weak one.
    """
    _ = constraints  # the engine folded the per-case results into validity_score
    return evidence.validity_score > 0.0


def validate_intervention(evidence: Evidence, plan: ExperimentPlan) -> bool:
    """Whether the probe was a usable test of the claim."""
    return evidence.validity_score >= plan.validity_threshold


def compare_baseline(evidence: Evidence) -> RunSummary:
    return evidence.baseline


def compare_intervention(evidence: Evidence) -> RunSummary:
    return evidence.intervention


def compare_control(evidence: Evidence) -> RunSummary | None:
    return evidence.control


def calculate_effect_size(evidence: Evidence) -> float:
    """Mean paired change from baseline to intervention, recomputed from the stored runs.

    Recomputed rather than trusted: the stored ``effect_size`` came from the engine, and the
    verifier's job includes not taking the engine's word for it.
    """
    return effect_size(paired_deltas(evidence.baseline.scores, evidence.intervention.scores))


def calculate_confidence_interval(evidence: Evidence) -> tuple[float, float] | None:
    return evidence.effect_ci


def calculate_reproducibility(evidence: Evidence, plan: ExperimentPlan) -> float:
    """Fraction of cases that behaved as the claim predicted, recomputed from stored runs."""
    deltas = paired_deltas(evidence.baseline.scores, evidence.intervention.scores)
    return reproducibility(deltas, plan.expected_direction, plan.min_effect_threshold)


def calculate_control_effect(evidence: Evidence) -> float | None:
    if evidence.control is None:
        return None
    return effect_size(paired_deltas(evidence.baseline.scores, evidence.control.scores))


def calculate_behavioral_support(observed_rate: float, validity: float) -> float:
    """How strongly the behavior supported the claim, discounted by how good the test was.

    A perfect reproduction rate obtained through a barely-valid intervention is not strong
    support, and multiplying keeps that honest instead of reporting 1.0.
    """
    return round(max(0.0, min(1.0, observed_rate * validity)), 9)


def detect_contradiction(
    *,
    observed_rate: float,
    reproducibility_threshold: float,
    effect: float,
    control_effect: float | None,
    min_effect_threshold: float,
    primacy_claim: bool,
) -> tuple[bool, list[str]]:
    """Is there positive evidence *against* the claim?

    Two independent ways a claim can be contradicted:

      1. The predicted effect is reproducibly **absent** - the intervention worked, and the
         model did not respond the way the explanation said it would.
      2. For a claim of *primacy*, a control variable moves the output more than the
         claimed driver does. The claimed effect may be perfectly real; it is simply not
         the primary one, which is what the explanation asserted.

    Note what is not here: "the effect was not observed" on its own. An unreproducible
    result is ambiguity, not refutation.
    """
    reasons: list[str] = []
    absence_rate = 1.0 - observed_rate
    reproducibly_absent = absence_rate >= reproducibility_threshold
    if reproducibly_absent:
        reasons.append(ReasonCode.EFFECT_REPRODUCIBLY_ABSENT)

    control_dominates = False
    if primacy_claim and control_effect is not None:
        margin = CONTROL_DOMINANCE_FRACTION * min_effect_threshold
        control_dominates = abs(control_effect) - abs(effect) >= margin
        if control_dominates:
            reasons.extend([ReasonCode.CONTROL_DOMINATES, ReasonCode.PRIMACY_REFUTED])

    return (reproducibly_absent or control_dominates), reasons


# --------------------------------------------------------------------------------------
# The verdict
# --------------------------------------------------------------------------------------


def verify(evidence: Evidence, claim: Claim, plan: ExperimentPlan) -> VerificationResult:
    """Decide the verdict. Pure, deterministic, and reproducible from the stored evidence."""
    _check_scope(evidence, claim, plan)

    validity = evidence.validity_score
    effect = calculate_effect_size(evidence)
    control_effect = calculate_control_effect(evidence)
    observed_rate = calculate_reproducibility(evidence, plan)
    observed_direction = direction_of(effect, plan.min_effect_threshold)
    deltas = paired_deltas(evidence.baseline.scores, evidence.intervention.scores)
    standardized = standardized_effect(deltas)
    n = evidence.baseline.n

    def result(
        status: VerdictStatus, reasons: list[str], contradiction: float = 0.0
    ) -> VerificationResult:
        codes = sorted(set(reasons))
        return VerificationResult(
            status=status,
            behavioral_support=calculate_behavioral_support(observed_rate, validity),
            intervention_validity=round(validity, 9),
            reproducibility=observed_rate,
            contradiction_score=round(max(0.0, min(1.0, contradiction)), 9),
            effect_size=effect,
            standardized_effect=standardized,
            control_effect_size=control_effect,
            expected_direction=plan.expected_direction,
            observed_direction=observed_direction,
            reason_codes=codes,
            rationale=_render_rationale(
                status=status,
                reasons=codes,
                effect=effect,
                control_effect=control_effect,
                observed_rate=observed_rate,
                validity=validity,
                n=n,
                instability=evidence.instability,
                plan=plan,
            ),
        )

    # -- Gate 1: was this a usable test at all? ----------------------------------------
    if not validate_intervention(evidence, plan):
        reasons = [ReasonCode.INVALID_INTERVENTION]
        if 0.0 < validity < plan.validity_threshold:
            reasons.append(ReasonCode.WEAK_PERTURBATION)
        if validity == 0.0:
            reasons.append(ReasonCode.CONSTRAINT_VIOLATION)
        return result(VerdictStatus.INCONCLUSIVE, reasons)

    # -- Gate 2: is the claim specific enough to be worth testing? ----------------------
    if claim.testability_score < MIN_TESTABILITY:
        return result(VerdictStatus.INCONCLUSIVE, [ReasonCode.VAGUE_CLAIM])

    # -- Gate 3: enough runs to say anything? ------------------------------------------
    if n < plan.min_repetitions_for_verdict:
        return result(VerdictStatus.INCONCLUSIVE, [ReasonCode.INSUFFICIENT_RUNS])

    # -- Gate 4: does the model even agree with itself? --------------------------------
    if evidence.instability > plan.instability_threshold:
        return result(VerdictStatus.INCONCLUSIVE, [ReasonCode.MODEL_UNSTABLE])

    # -- Now, and only now, look at what the data showed -------------------------------
    contradicted, contradiction_reasons = detect_contradiction(
        observed_rate=observed_rate,
        reproducibility_threshold=plan.reproducibility_threshold,
        effect=effect,
        control_effect=control_effect,
        min_effect_threshold=plan.min_effect_threshold,
        primacy_claim=claim.primacy_claim,
    )

    effect_reproducible = observed_rate >= plan.reproducibility_threshold

    if contradicted:
        absence_rate = 1.0 - observed_rate
        dominance = 0.0
        if control_effect is not None and abs(control_effect) > 0:
            dominance = max(
                0.0, (abs(control_effect) - abs(effect)) / abs(control_effect)
            )
        score = max(absence_rate if not effect_reproducible else 0.0, dominance)
        return result(
            VerdictStatus.CONTRADICTED,
            [*contradiction_reasons, ReasonCode.VALID_INTERVENTION],
            contradiction=score,
        )

    if effect_reproducible:
        reasons = [ReasonCode.EFFECT_REPRODUCIBLE, ReasonCode.VALID_INTERVENTION]
        if control_effect is None:
            reasons.append(ReasonCode.CONTROL_ABSENT)
        if observed_direction is not plan.expected_direction:
            # Reproducible, but in the wrong direction: that is not support.
            return result(
                VerdictStatus.INCONCLUSIVE,
                [ReasonCode.DIRECTION_MISMATCH, ReasonCode.VALID_INTERVENTION],
            )
        return result(VerdictStatus.SUPPORTED, reasons)

    # Neither reproducibly present nor reproducibly absent.
    return result(
        VerdictStatus.INCONCLUSIVE,
        [ReasonCode.EFFECT_NOT_REPRODUCIBLE, ReasonCode.VALID_INTERVENTION],
    )


def generate_verdict(
    evidence: Evidence,
    claim: Claim,
    plan: ExperimentPlan,
    *,
    created_at: datetime,
) -> Verdict:
    """Run verification and package it as a persistable Verdict record."""
    outcome = verify(evidence, claim, plan)
    return Verdict(
        id=verdict_id(claim.id, [evidence.id], VERIFIER_VERSION),
        claim_id=claim.id,
        claim_family_id=claim.claim_family_id,
        scope=claim.scope,
        protocol_version=plan.protocol_version,
        status=outcome.status,
        behavioral_support=outcome.behavioral_support,
        intervention_validity=outcome.intervention_validity,
        reproducibility=outcome.reproducibility,
        contradiction_score=outcome.contradiction_score,
        effect_size=outcome.effect_size,
        control_effect_size=outcome.control_effect_size,
        expected_direction=outcome.expected_direction,
        observed_direction=outcome.observed_direction,
        evidence_ids=[evidence.id],
        reason_codes=outcome.reason_codes,
        rationale=outcome.rationale,
        verifier_version=VERIFIER_VERSION,
        created_at=created_at,
    )


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _check_scope(evidence: Evidence, claim: Claim, plan: ExperimentPlan) -> None:
    """Refuse to verify artifacts that are not talking about the same thing."""
    if evidence.claim_id != claim.id:
        raise ValidationError(
            f"evidence {evidence.id} belongs to claim {evidence.claim_id}, not {claim.id}"
        )
    if evidence.experiment_id != plan.id:
        raise ValidationError(
            f"evidence {evidence.id} came from experiment {evidence.experiment_id}, "
            f"not {plan.id}"
        )
    if not (evidence.scope.matches(claim.scope) and evidence.scope.matches(plan.scope)):
        raise ValidationError(
            f"scope mismatch: evidence={evidence.scope.label()}, "
            f"claim={claim.scope.label()}, plan={plan.scope.label()}"
        )
    if evidence.baseline.kind is not RunKind.BASELINE:
        raise ValidationError("evidence baseline arm is mislabelled")


def _render_rationale(
    *,
    status: VerdictStatus,
    reasons: list[str],
    effect: float,
    control_effect: float | None,
    observed_rate: float,
    validity: float,
    n: int,
    instability: float,
    plan: ExperimentPlan,
) -> str:
    """Compose the rationale from structured values only.

    Every number here is one the verifier computed. Nothing is narrated by a model, so the
    sentence cannot drift from the values it claims to describe.
    """
    control_text = (
        f"{control_effect:+.4f}" if control_effect is not None else "not run"
    )
    return (
        f"{status}: effect={effect:+.4f} (threshold {plan.min_effect_threshold:.2f}), "
        f"control_effect={control_text}, reproducibility={observed_rate:.3f} "
        f"(threshold {plan.reproducibility_threshold:.2f}), "
        f"intervention_validity={validity:.3f} "
        f"(threshold {plan.validity_threshold:.2f}), runs={n}, "
        f"instability={instability:.4f}, verifier={VERIFIER_VERSION}. "
        f"Reasons: {', '.join(reasons)}."
    )
