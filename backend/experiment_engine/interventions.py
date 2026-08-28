"""Applying and validating interventions.

This module is where a language model's suggestion stops being a suggestion. The
Experimenter agent may propose *which* variable to perturb and *how*; this code decides
whether that proposal is a legitimate experiment, applies it, and then checks the result
against what was declared.

The distinction that matters: an intervention can fail in two very different ways.

  - It can be **invalid** - it changed something it promised to preserve, pushed a feature
    outside its realistic range, or perturbed a variable so little that nothing could have
    been learned. Invalid interventions produce INCONCLUSIVE, never CONTRADICTED, because
    a broken test tells you nothing about the claim.
  - It can be **valid but unrevealing** - a clean intervention that simply did not move the
    output. That is real evidence, and it is what CONTRADICTED is for.

Conflating those two is the single easiest way to manufacture false contradictions, so
validity is computed here, separately, before any verdict logic runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.core.enums import InterventionType
from backend.core.errors import InterventionRejected
from backend.core.schemas import ConstraintSpec, InterventionSpec
from backend.experiment_engine.distributions import FEATURE_INDEX, feature_spec

MIN_PERTURBATION_FRACTION: float = 0.15
"""An intervention must move its target by at least 15% of the feature's range to count as
a real perturbation. Below that, the probe cannot distinguish "the feature does not matter"
from "we barely touched it" - which is exactly the trap a distribution shift sets."""


@dataclass(frozen=True, slots=True)
class ValidityReport:
    """Why an intervention is or is not a usable test.

    ``score`` is the number the verifier gates on. ``reason_codes`` is what a human reads.
    """

    score: float
    scope_respected: bool
    constraints_preserved: bool
    within_range: bool
    perturbation: float
    adequacy: float
    reason_codes: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def is_hard_failure(self) -> bool:
        """True when the probe was malformed rather than merely weak."""
        return not (self.scope_respected and self.constraints_preserved and self.within_range)


def resolve_target_value(spec: InterventionSpec, current: float) -> float:
    """The value a feature takes after the intervention is applied.

    NEUTRALIZE and SUBSTITUTE are absolute; INCREASE and DECREASE are relative; ABLATION
    drives the feature to the bottom of its declared range. The schema already guaranteed
    the required parameter is present, so this stays a straight mapping.
    """
    feature = feature_spec(spec.variable)
    match spec.intervention_type:
        case InterventionType.NEUTRALIZE | InterventionType.SUBSTITUTE:
            assert spec.value is not None  # guaranteed by InterventionSpec validation
            return float(spec.value)
        case InterventionType.INCREASE | InterventionType.DECREASE:
            assert spec.delta is not None
            return current + float(spec.delta)
        case InterventionType.ABLATION:
            return feature.minimum
    raise InterventionRejected(f"unsupported intervention type {spec.intervention_type}")


def apply_intervention(
    features: dict[str, float], spec: InterventionSpec, *, clamp: bool = True
) -> dict[str, float]:
    """Return a new feature vector with the intervention applied.

    The input is never mutated: the baseline vector has to survive unchanged so the paired
    comparison is against what was actually run, not against a vector something else edited.
    """
    if spec.variable not in FEATURE_INDEX:
        raise InterventionRejected(
            f"cannot intervene on unknown feature {spec.variable!r}; "
            f"the laboratory defines {sorted(FEATURE_INDEX)}"
        )
    if spec.variable not in features:
        raise InterventionRejected(
            f"feature vector has no {spec.variable!r} to intervene on"
        )

    feature = feature_spec(spec.variable)
    target = resolve_target_value(spec, float(features[spec.variable]))
    if clamp:
        target = min(feature.maximum, max(feature.minimum, target))

    updated = dict(features)
    updated[spec.variable] = round(target, 9)
    return updated


def validate_intervention(
    *,
    baseline: dict[str, float],
    intervened: dict[str, float],
    spec: InterventionSpec,
    constraints: ConstraintSpec,
) -> ValidityReport:
    """Check an applied intervention against everything the plan promised.

    Called on the *actual executed vectors*, not on the plan. Validating the plan alone
    would only prove the plan was well-written; this proves the experiment that ran was the
    experiment that was described.
    """
    reasons: list[str] = []
    details: list[str] = []

    # 1. Scope: exactly one variable may differ, and it must be the declared one.
    changed = {
        name
        for name in set(baseline) | set(intervened)
        if abs(float(baseline.get(name, 0.0)) - float(intervened.get(name, 0.0))) > 1e-12
    }
    unexpected = changed - {spec.variable}
    scope_respected = not unexpected
    if unexpected:
        reasons.append("OUT_OF_SCOPE_MUTATION")
        details.append(f"changed features outside the target: {sorted(unexpected)}")

    # 2. Preservation: everything declared preserved must hold within tolerance.
    violations: list[str] = []
    for name in constraints.preserved_features:
        before = float(baseline.get(name, float("nan")))
        after = float(intervened.get(name, float("nan")))
        if before != before or after != after:  # NaN means the feature is absent
            violations.append(f"{name}=<missing>")
            continue
        if abs(before - after) > constraints.tolerance:
            violations.append(f"{name}: {before} -> {after}")
    constraints_preserved = not violations
    if violations:
        reasons.append("CONSTRAINT_VIOLATION")
        details.append(f"preserved features moved: {violations}")

    # 3. Range: the post-intervention vector must still be a realistic input.
    within_range = True
    if constraints.require_realistic_range:
        for name, value in intervened.items():
            spec_for_name = FEATURE_INDEX.get(name)
            if spec_for_name is None:
                within_range = False
                reasons.append("UNKNOWN_FEATURE")
                details.append(f"{name!r} is not a laboratory feature")
                continue
            bounds = constraints.feature_bounds.get(
                name, (spec_for_name.minimum, spec_for_name.maximum)
            )
            if not (bounds[0] <= float(value) <= bounds[1]):
                within_range = False
                reasons.append("OUT_OF_RANGE")
                details.append(f"{name}={value} outside {bounds}")

    # 4. Adequacy: did we actually move the thing we claim to be testing?
    perturbation = abs(
        float(intervened.get(spec.variable, 0.0)) - float(baseline.get(spec.variable, 0.0))
    )
    feature = FEATURE_INDEX.get(spec.variable)
    span = (feature.maximum - feature.minimum) if feature else 1.0
    required = MIN_PERTURBATION_FRACTION * span
    adequacy = min(1.0, perturbation / required) if required > 0 else 1.0
    if adequacy < 1.0:
        reasons.append("WEAK_PERTURBATION")
        details.append(
            f"moved {spec.variable} by {perturbation:.4f}; a meaningful probe needs "
            f"{required:.4f} on this feature"
        )

    hard_failure = not (scope_respected and constraints_preserved and within_range)
    score = 0.0 if hard_failure else round(adequacy, 6)

    if not reasons:
        reasons.append("VALID_INTERVENTION")

    return ValidityReport(
        score=score,
        scope_respected=scope_respected,
        constraints_preserved=constraints_preserved,
        within_range=within_range,
        perturbation=round(perturbation, 9),
        adequacy=round(adequacy, 6),
        reason_codes=sorted(set(reasons)),
        detail=(
            "; ".join(details)
            if details
            else "intervention satisfied every declared constraint"
        ),
    )


def aggregate_validity(reports: list[ValidityReport]) -> ValidityReport:
    """Combine per-case validity into one report for the experiment.

    The two kinds of failure are aggregated differently, and the difference is deliberate:

      - **Hard failures** (out-of-scope mutation, constraint violation, out-of-range input)
        are combined with AND. One malformed case means the protocol itself is broken, and
        averaging would let a majority of clean cases hide it.
      - **Adequacy** is averaged. It is a continuous measure of how hard the probe pushed,
        and a single case that happened to start near the neutral value does not invalidate
        twenty-three good ones - it just contributes a near-zero delta, which the paired
        statistics already account for.

    Taking the minimum of both would report a strong probe as worthless whenever one case
    sat near the neutral point, which is a real configuration and not an error.
    """
    if not reports:
        raise InterventionRejected("cannot aggregate validity with no cases")

    scope_respected = all(r.scope_respected for r in reports)
    constraints_preserved = all(r.constraints_preserved for r in reports)
    within_range = all(r.within_range for r in reports)
    hard_failure = not (scope_respected and constraints_preserved and within_range)

    mean_adequacy = sum(r.adequacy for r in reports) / len(reports)
    mean_perturbation = sum(r.perturbation for r in reports) / len(reports)

    codes = sorted({code for report in reports for code in report.reason_codes})
    if len(codes) > 1 and "VALID_INTERVENTION" in codes:
        codes.remove("VALID_INTERVENTION")

    worst = min(reports, key=lambda r: r.score)
    return ValidityReport(
        score=0.0 if hard_failure else round(mean_adequacy, 6),
        scope_respected=scope_respected,
        constraints_preserved=constraints_preserved,
        within_range=within_range,
        perturbation=round(mean_perturbation, 9),
        adequacy=round(mean_adequacy, 6),
        reason_codes=codes,
        detail=worst.detail,
    )
