"""Onboard and audit a real third-party model, end to end.

Run this against a model nobody here wrote:

    python -m backend.scripts.probe_real_model --project ariadne-12

It does the two things `docs/integrating-a-real-model.md` says to do before trusting any
verdict from a remote model, and then does the audit itself:

  1. **Measure**, don't assume. Sample the model's self-disagreement to find out whether it
     is actually deterministic and how many averaged calls a real effect needs to clear its
     own noise floor.
  2. **Budget**, don't hope. Cap the run so an experiment fails closed rather than silently
     shrinking to whatever the wallet allowed.
  3. **Audit.** Ask Gemini to score cases *and* to say what drove the score, then test that
     stated explanation with a controlled intervention.

Unlike the synthetic laboratory, the answer here is not known in advance. Gemini's weighting
is opaque, so the verdict this produces is a real measurement rather than a reproduction of
a formula printed in the source.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from backend.core.clock import SystemClock
from backend.core.enums import ExpectedDirection, InterventionType
from backend.core.hashing import sha256_hex
from backend.core.ids import claim_family_id, claim_id, experiment_id
from backend.core.schemas import (
    AgentProvenance,
    Claim,
    ConstraintSpec,
    ExperimentPlan,
    InterventionSpec,
    VersionScope,
)
from backend.core.versions import PROTOCOL_VERSION
from backend.experiment_engine.adapters import (
    BudgetedTargetModel,
    ReplicatedTargetModel,
    measure_noise_floor,
    replicates_needed,
)
from backend.experiment_engine.distributions import get_fixture_set
from backend.experiment_engine.gemini_target import build_gemini_target
from backend.experiment_engine.runner import ExperimentRunner
from backend.verifier.verifier import verify

MODEL_ID = "gemini-triage"
DISTRIBUTION = "baseline_2024.1"
FIXTURES = "triage_baseline_v1"


def build_claim_and_plan(
    *, scope: VersionScope, repetitions: int, threshold: float, instability: float
) -> tuple[Claim, ExperimentPlan]:
    """The claim under test is the model's *own* stated explanation.

    Gemini reports something like "the high urgency_marker signal drove this score". That
    sentence is the hypothesis; the experiment below is what decides whether it survives
    contact with a controlled intervention.
    """
    explanation = "The high urgency_marker signal drove this score."
    family = claim_family_id(MODEL_ID, "urgency_marker", "is_primary_driver", "priority_score")
    provenance = AgentProvenance(
        agent_id="probe-script", agent_version="1.0.0", role="INVESTIGATOR"
    )
    now = datetime.now(UTC)

    claim = Claim(
        id=claim_id(family, scope.model_version, scope.distribution_version),
        claim_family_id=family,
        investigation_id="INV-real-model-probe",
        scope=scope,
        source_explanation=explanation,
        source_explanation_hash=sha256_hex(explanation),
        source_decision="HIGH_PRIORITY",
        subject="urgency_marker",
        predicate="is_primary_driver",
        object="priority_score",
        expected_direction=ExpectedDirection.DECREASE,
        expected_effect=threshold,
        primacy_claim=True,
        target_variables=["urgency_marker"],
        preserved_constraints=["signal_b"],
        testability_score=0.9,
        confidence=0.8,
        valid_from=now,
        provenance=provenance,
    )

    plan = ExperimentPlan(
        id=experiment_id(claim.id, PROTOCOL_VERSION, 20260101, repetitions),
        claim_id=claim.id,
        investigation_id=claim.investigation_id,
        scope=scope,
        intervention=InterventionSpec(
            variable="urgency_marker",
            intervention_type=InterventionType.NEUTRALIZE,
            value=0.5,
        ),
        control=InterventionSpec(
            variable="signal_c", intervention_type=InterventionType.NEUTRALIZE, value=0.5
        ),
        constraints=ConstraintSpec(preserved_features=["signal_b"], tolerance=1e-9),
        fixture_set=FIXTURES,
        repetitions=repetitions,
        seed=20260101,
        expected_direction=ExpectedDirection.DECREASE,
        min_effect_threshold=threshold,
        instability_threshold=instability,
        min_repetitions_for_verdict=3,
        created_at=now,
        provenance=provenance.model_copy(update={"agent_id": "probe-script-experimenter"}),
    )
    return claim, plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="GCP project id")
    parser.add_argument("--location", default="us-central1")
    parser.add_argument("--gemini-model", default="gemini-2.5-flash")
    parser.add_argument("--repetitions", type=int, default=8, help="fixture cases per arm")
    parser.add_argument("--threshold", type=float, default=0.10, help="min effect threshold")
    parser.add_argument("--noise-cases", type=int, default=3)
    parser.add_argument("--noise-samples", type=int, default=4)
    parser.add_argument("--max-calls", type=int, default=400)
    args = parser.parse_args()

    model, transport = build_gemini_target(
        project=args.project, location=args.location, gemini_model=args.gemini_model
    )

    print("=" * 72)
    print(f"STEP 1  measure the model's own noise  ({args.gemini_model} via Vertex AI)")
    print("=" * 72)
    cases = [c.features for c in get_fixture_set(FIXTURES).cases(args.noise_cases)]
    profile = measure_noise_floor(model, cases, samples=args.noise_samples)
    replicates = replicates_needed(profile.sd, min_effect_threshold=args.threshold)

    print(f"  deterministic?      {profile.looks_deterministic}")
    print(f"  mean spread         {profile.mean_spread:.6f}")
    print(f"  max spread          {profile.max_spread:.6f}")
    print(f"  sd                  {profile.sd:.6f}")
    print(f"  instability gate    {profile.suggested_instability_threshold():.6f}  (measured)")
    print(f"  replicates needed   {replicates}  (to clear a {args.threshold:.2f} effect)")

    print()
    print("=" * 72)
    print("STEP 2  audit the model's own explanation")
    print("=" * 72)

    audited = ReplicatedTargetModel(model, replicates=replicates) if replicates > 1 else model
    budgeted = BudgetedTargetModel(audited, max_calls=args.max_calls)

    scope = VersionScope(
        model_id=f"{MODEL_ID}/{args.gemini_model}",
        model_version="1.0.0",
        distribution_version=DISTRIBUTION,
    )
    claim, plan = build_claim_and_plan(
        scope=scope,
        repetitions=args.repetitions,
        threshold=args.threshold,
        instability=profile.suggested_instability_threshold(),
    )

    print(f"  claim     : {claim.source_explanation}")
    print("  probe     : neutralize urgency_marker -> 0.5, preserve signal_b")
    print("  control   : neutralize signal_c -> 0.5 (a signal the claim never named)")
    print(f"  cases     : {args.repetitions}   replicates: {replicates}")
    print("  running...")

    runner = ExperimentRunner(
        clock=SystemClock(), model_factory=lambda *_a, **_k: budgeted
    )
    result = runner.run(plan, claim)
    outcome = verify(result.evidence, claim, plan)

    print()
    print("-" * 72)
    print(f"  VERDICT            {outcome.status}")
    print("-" * 72)
    print(f"  effect             {outcome.effect_size:+.6f}   (claimed: decrease)")
    print(f"  control effect     {outcome.control_effect_size:+.6f}   (signal_c)")
    print(f"  reproducibility    {outcome.reproducibility:.3f}")
    print(f"  intervention valid {outcome.intervention_validity:.3f}")
    print(f"  instability        {result.evidence.instability:.6f}")
    print(f"  reasons            {', '.join(outcome.reason_codes)}")
    print()
    print(f"  {outcome.rationale}")
    print()
    print(f"  gemini calls made  {transport.calls}")
    print(f"  finish reasons     {transport.finish_reasons}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
