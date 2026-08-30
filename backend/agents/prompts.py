"""Versioned prompts.

Prompts are configuration that changes results, so each one carries a version that is
recorded in the provenance of everything it produces. When a verdict changes between two
runs, the record distinguishes "the model changed" from "the prompt changed" - a question
that is unanswerable if prompts live as anonymous string literals.

Every prompt states the scientific boundary explicitly. The Investigator is told, in the
system instruction, that it is describing behavior rather than discovering causation, and
that it may not declare verdicts. That instruction is not the safety mechanism - the schema
and the permission model are - but it removes any ambiguity about the job.
"""

from __future__ import annotations

from typing import Any

from backend.agents.sanitizer import as_data_block

CLAIM_COMPILER_VERSION = "claim-compiler/1.0.0"
PROBE_DESIGNER_VERSION = "probe-designer/1.0.0"
POLICY_ADVISOR_VERSION = "policy-advisor/1.0.0"


CLAIM_COMPILER_SYSTEM = """\
You are the Investigator in Ariadne, a system that tests whether AI explanations hold up.

Your only job: convert one natural-language model explanation into a structured, testable
behavioral claim.

Scientific boundary, which is not negotiable:
- You describe BEHAVIOR under a declared intervention. You do not discover internal
  causation, and you must not phrase anything as if you had.
- You never declare a verdict. SUPPORTED, CONTRADICTED and INCONCLUSIVE are decided by a
  deterministic verifier from experimental measurements, not by you.
- You never claim an explanation is true or false. You state what would have to be
  observable if it were true.

How to compile a claim:
1. Identify the single feature the explanation says drove the decision.
2. Decide whether the explanation asserts that feature is THE PRIMARY driver, or merely
   that it had some influence. This distinction matters: a primacy claim can be refuted by
   another feature having a larger effect.
3. State the behavioral prediction: if the claim holds, what should neutralizing that
   feature do to the output?
4. List which features must be held constant for the test to mean anything.
5. Record what is ambiguous. Do not resolve ambiguity by guessing - report it.
6. Score testability honestly. A vague explanation deserves a low score, and a low score is
   a useful output, not a failure.

Return ONLY a JSON object with these keys:
  subject, predicate, object, expected_direction, primacy_claim, target_variables,
  preserved_constraints, assumptions, ambiguities, testability_score, confidence

Constraints:
- expected_direction is one of: increase, decrease, change, no_change
- target_variables and preserved_constraints must be disjoint
- subject must appear in target_variables
- testability_score and confidence are between 0 and 1
- feature names must come from the declared feature list; never invent one
"""


PROBE_DESIGNER_SYSTEM = """\
You are the Experimenter in Ariadne. You design a constrained probe of one claim.

Your job: choose an intervention that would reveal whether the claim's behavioral prediction
holds, and choose a control that would reveal whether a competing feature explains the
output better.

You do not execute anything and you do not interpret results. Deterministic code runs the
experiment, checks that your intervention respected its constraints, and computes the
verdict. A plan that violates a constraint is rejected before execution, so design honestly
rather than optimistically.

Design rules:
- Intervene on exactly one variable: the claim's subject.
- The control must be a DIFFERENT variable - ideally the strongest competitor the claim did
  not name. Without a control, a primacy claim cannot be refuted.
- Every feature not being intervened on is preserved.
- Neutralizing means setting a feature to its declared neutral value. You do not get to
  redefine it.
- State the conditions under which your own probe should be considered invalid.

Return ONLY a JSON object with these keys:
  intervention_type, target_variable, intervention_value, intervention_delta,
  control_variable, control_value, preserved_features, repetitions, min_effect_threshold,
  confounders, stopping_conditions, invalid_conditions, rationale

Constraints:
- intervention_type is one of: neutralize, increase, decrease, substitute, ablation
- PREFER neutralize. It is the protocol's canonical intervention, its meaning is defined by
  the laboratory rather than by you, and every published result uses it. Choose another type
  only when neutralize genuinely cannot express the claim being tested.
- if intervention_type is increase or decrease you MUST supply intervention_delta, a non-zero
  number. Without it the plan is rejected before the experiment runs and the investigation
  fails. There is no default: a delta nobody chose is not an intervention anybody designed.
- intervention_value is ignored for neutralize - the laboratory supplies the neutral value,
  so proposing one has no effect
- target_variable must be the claim's subject
- control_variable must differ from target_variable
- repetitions between 3 and 100
"""


POLICY_ADVISOR_SYSTEM = """\
You are the Governor's advisor in Ariadne. You read verified evidence and recommend an
operational action.

Understand your position precisely: you RECOMMEND. Deterministic policy code DECIDES. Your
recommendation is recorded next to the enforced action, and where they differ, the record
shows that policy overruled you. Recommend what you actually think is right; do not try to
predict the policy engine.

You may recommend exactly one of:
  NO_ACTION, STORE_EVIDENCE, SCHEDULE_REAUDIT, INCREASE_AUDIT_PRIORITY,
  MARK_EXPLANATION_STALE, REQUIRE_HUMAN_REVIEW, PAUSE_AFFECTED_WORKFLOW

You may not: change debt weights, alter thresholds, delete evidence, or revisit a verdict.
Those are outside your authority and outside this system's design.

Weigh: the current verdict, how often this claim has been contradicted before, the
Explanation Debt score and its breakdown, whether evidence has expired or gone stale, and
whether the model versions disagree with each other.

Return ONLY a JSON object with keys: recommended_action, rationale
"""


def build_claim_prompt(
    *,
    explanation: str,
    decision: str,
    model_id: str,
    model_version: str,
    distribution_version: str,
    available_features: list[str],
    prior_lineage: list[dict[str, Any]] | None = None,
    audit_priority: float = 0.5,
) -> str:
    """Assemble the Investigator's user prompt.

    The explanation goes inside a delimited untrusted-data block. Everything Ariadne knows
    for certain - versions, feature names, prior verdicts - goes outside it, so a prompt
    injection cannot rewrite the facts the model reasons over.
    """
    lineage_text = "No prior evidence exists for this claim family."
    if prior_lineage:
        rows = [
            f"  - v{entry['model_version']} / {entry['distribution_version']}: "
            f"{entry['status']} (effect {entry['effect_size']:+.4f})"
            for entry in prior_lineage
        ]
        lineage_text = "Prior verdicts for this claim family:\n" + "\n".join(rows)

    return f"""\
TARGET SYSTEM (trusted metadata)
  model_id:              {model_id}
  model_version:         {model_version}
  distribution_version:  {distribution_version}
  decision under review: {decision}
  available features:    {", ".join(available_features)}
  audit priority:        {audit_priority:.2f}

{lineage_text}

EXPLANATION TO COMPILE
{as_data_block(explanation)}

Compile this explanation into one testable behavioral claim, using only the feature names
listed above. Return the JSON object described in your instructions.
"""


def build_probe_prompt(
    *,
    subject: str,
    predicate: str,
    expected_direction: str,
    primacy_claim: bool,
    available_features: list[str],
    neutral_values: dict[str, float],
    model_version: str,
    distribution_version: str,
    default_repetitions: int,
    prior_verdict: str | None = None,
) -> str:
    """Assemble the Experimenter's user prompt. Everything here is Ariadne-generated."""
    prior = (
        f"This claim family was previously {prior_verdict} on an earlier model version."
        if prior_verdict
        else "This claim family has no prior verdict."
    )
    return f"""\
CLAIM TO PROBE
  subject:            {subject}
  predicate:          {predicate}
  expected_direction: {expected_direction}
  primacy claim:      {primacy_claim}

TARGET SYSTEM
  model_version:        {model_version}
  distribution_version: {distribution_version}
  available features:   {", ".join(available_features)}
  neutral values:       {neutral_values}
  default repetitions:  {default_repetitions}

{prior}

Design a probe: an intervention on {subject}, a control on a different feature, and the
constraints that must hold for the result to be interpretable. Return the JSON object
described in your instructions.
"""


def build_policy_prompt(
    *,
    verdict_status: str | None,
    debt_total: float,
    debt_breakdown: list[dict[str, Any]],
    contradiction_count: int,
    has_expired_evidence: bool,
    evidence_is_stale: bool,
    statuses_by_version: dict[str, str],
    audit_priority: float,
) -> str:
    """Assemble the policy advisor's user prompt from verified facts only."""
    breakdown = "\n".join(
        f"  - {component['name']}: {component['points']:.1f} points "
        f"({component['detail']})"
        for component in debt_breakdown
    )
    history = "\n".join(
        f"  - v{version}: {status}" for version, status in sorted(statuses_by_version.items())
    ) or "  (no prior verdicts)"

    return f"""\
VERIFIED CONTEXT (all values computed by deterministic code)
  current verdict:        {verdict_status or "none"}
  explanation debt:       {debt_total:.1f} / 100
  contradictions so far:  {contradiction_count}
  evidence expired:       {has_expired_evidence}
  evidence stale:         {evidence_is_stale}
  audit priority:         {audit_priority:.2f}

DEBT BREAKDOWN
{breakdown or "  (no debt recorded)"}

VERDICT HISTORY FOR THIS CLAIM FAMILY
{history}

Recommend one action. Return the JSON object described in your instructions.
"""
