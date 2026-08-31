"""Experiment execution.

The runner is deliberately dull. It takes a validated plan, executes it against the target
model, and records what happened. It does not interpret results, and it has no access to
the verdict vocabulary at all - the Experimenter role "cannot declare a verdict" is
enforced by this module simply having no way to express one.

Three properties it must guarantee:

  1. **Version binding.** A plan scoped to v1 never runs against v2. A mis-scoped result is
     worse than no result, because it looks valid.
  2. **Resumability.** Every run is recorded as it completes. A worker that dies halfway
     resumes from the recorded runs instead of re-executing them, so a retried event cannot
     silently double the sample size.
  3. **Provenance.** Every call records the hash of what went in and what came out.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from backend.core.clock import Clock, SystemClock
from backend.core.enums import RunKind
from backend.core.errors import (
    AriadneError,
    InterventionRejected,
    TargetModelError,
    ValidationError,
    VersionMismatch,
)
from backend.core.hashing import sha256_hex
from backend.core.ids import evidence_id, run_id
from backend.core.schemas import Claim, Evidence, ExperimentPlan, ExperimentRun, RunSummary
from backend.experiment_engine.distributions import FixtureCase, get_fixture_set
from backend.experiment_engine.interventions import (
    ValidityReport,
    aggregate_validity,
    apply_intervention,
    validate_intervention,
)
from backend.experiment_engine.target_model import TargetModel, get_target_model
from backend.verifier.statistics import (
    bootstrap_ci,
    effect_size,
    instability,
    paired_deltas,
    reproducibility,
    summarize,
)

REPLICATE_PROBES: int = 2
"""How many cases are executed twice to measure target-model instability. Two is enough to
catch a model that answers differently on identical input, and cheap enough to always run."""


class RunStore(Protocol):
    """Checkpoint storage for individual runs.

    Narrow by design: the runner needs to know what already happened and to record what
    just happened. Everything else about persistence is somebody else's problem.
    """

    def completed_runs(self, experiment_id: str) -> dict[str, ExperimentRun]: ...

    def record_run(self, run: ExperimentRun) -> None: ...


class InMemoryRunStore:
    """Non-durable RunStore. Fine for a single process; useless across a crash."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, ExperimentRun]] = {}

    def completed_runs(self, experiment_id: str) -> dict[str, ExperimentRun]:
        return dict(self._runs.get(experiment_id, {}))

    def record_run(self, run: ExperimentRun) -> None:
        self._runs.setdefault(run.experiment_id, {})[run.id] = run


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    """Everything one experiment produced. Measurements only - no conclusion."""

    evidence: Evidence
    runs: list[ExperimentRun]
    validity: ValidityReport
    cases_executed: int
    runs_reused: int
    """How many runs were restored from a checkpoint instead of re-executed. Non-zero after
    a resume, and the number the crash-recovery test asserts on."""


class ExperimentRunner:
    """Executes an ExperimentPlan against a target model."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        model_factory=get_target_model,
        run_store: RunStore | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._model_factory = model_factory
        self._runs = run_store or InMemoryRunStore()

    # -- public ------------------------------------------------------------------------

    def run(self, plan: ExperimentPlan, claim: Claim) -> ExperimentResult:
        """Execute the plan. Raises before touching the model if the plan is not runnable."""
        self._check_plan(plan, claim)
        model = self._resolve_model(plan)

        cases = self._select_cases(plan)
        checkpoint = self._runs.completed_runs(plan.id)
        reused_before = len(checkpoint)

        baseline_runs: list[ExperimentRun] = []
        intervention_runs: list[ExperimentRun] = []
        control_runs: list[ExperimentRun] = []
        validity_reports: list[ValidityReport] = []

        for index, case in enumerate(cases):
            baseline_features = dict(case.features)
            baseline_runs.append(
                self._execute(model, plan, RunKind.BASELINE, index, baseline_features, checkpoint)
            )

            intervened = apply_intervention(baseline_features, plan.intervention)
            validity_reports.append(
                validate_intervention(
                    baseline=baseline_features,
                    intervened=intervened,
                    spec=plan.intervention,
                    constraints=plan.constraints,
                )
            )
            intervention_runs.append(
                self._execute(
                    model, plan, RunKind.INTERVENTION, index, intervened, checkpoint
                )
            )

            if plan.control is not None:
                control_features = apply_intervention(baseline_features, plan.control)
                control_runs.append(
                    self._execute(
                        model, plan, RunKind.CONTROL, index, control_features, checkpoint
                    )
                )

        validity = aggregate_validity(validity_reports)
        model_instability = self._measure_instability(model, plan, cases)
        evidence = self._assemble_evidence(
            plan=plan,
            claim=claim,
            baseline_runs=baseline_runs,
            intervention_runs=intervention_runs,
            control_runs=control_runs,
            validity=validity,
            model_instability=model_instability,
        )

        all_runs = [*baseline_runs, *intervention_runs, *control_runs]
        reused = sum(1 for run in all_runs if run.id in checkpoint)
        return ExperimentResult(
            evidence=evidence,
            runs=all_runs,
            validity=validity,
            cases_executed=len(cases),
            runs_reused=reused if reused_before else 0,
        )

    # -- plan admission ----------------------------------------------------------------

    def _check_plan(self, plan: ExperimentPlan, claim: Claim) -> None:
        """Everything that must be true before a single target-model call is made."""
        if plan.claim_id != claim.id:
            raise ValidationError(
                f"plan {plan.id} targets claim {plan.claim_id}, was given {claim.id}"
            )
        if not plan.scope.matches(claim.scope):
            raise VersionMismatch(
                f"plan is scoped to {plan.scope.label()} but the claim is scoped to "
                f"{claim.scope.label()}; a result computed across that gap would be "
                f"attributed to the wrong model"
            )
        if plan.intervention.variable not in claim.target_variables:
            raise InterventionRejected(
                f"plan intervenes on {plan.intervention.variable!r}, which the claim never "
                f"named as a target variable {claim.target_variables}"
            )
        if plan.control is not None and plan.control.variable in claim.target_variables:
            raise InterventionRejected(
                f"the control perturbs {plan.control.variable!r}, which is one of the "
                f"claim's own target variables; that is not a control"
            )
        if claim.quarantined:
            raise InterventionRejected(
                f"claim {claim.id} is quarantined and must not be executed: "
                f"{claim.quarantine_reasons}"
            )

    def _resolve_model(self, plan: ExperimentPlan) -> TargetModel:
        """Resolve by identity, not just by version.

        The factory used to receive `(version, distribution)` and no `model_id`, so it could
        not distinguish one organisation's model from the built-in laboratory even in
        principle. For a single-model demo that was invisible; once models became a
        registered resource it became the most dangerous defect available here - a confident
        verdict about the wrong model, scoped and recorded as though it described the right
        one.

        A factory that accepts the identity gets it. The three-argument form is tried first
        and the older two-argument form still works, so every existing caller and test is
        unaffected.
        """
        try:
            model = self._model_factory(
                plan.scope.model_version,
                plan.scope.distribution_version,
                model_id=plan.scope.model_id,
            )
        except TypeError:
            model = self._model_factory(
                plan.scope.model_version, plan.scope.distribution_version
            )
        if model.version != plan.scope.model_version:
            raise VersionMismatch(
                f"resolved model is v{model.version}, plan requires v{plan.scope.model_version}"
            )
        if model.distribution_version != plan.scope.distribution_version:
            raise VersionMismatch(
                f"resolved model uses distribution {model.distribution_version!r}, plan "
                f"requires {plan.scope.distribution_version!r}"
            )
        return model

    def _select_cases(self, plan: ExperimentPlan) -> list[FixtureCase]:
        fixtures = get_fixture_set(plan.fixture_set)
        if fixtures.distribution_version != plan.scope.distribution_version:
            raise VersionMismatch(
                f"fixture set {plan.fixture_set!r} draws from "
                f"{fixtures.distribution_version!r}, but the plan is scoped to "
                f"{plan.scope.distribution_version!r}"
            )
        return fixtures.cases(plan.repetitions)

    # -- execution ---------------------------------------------------------------------

    def _execute(
        self,
        model: TargetModel,
        plan: ExperimentPlan,
        kind: RunKind,
        index: int,
        features: dict[str, float],
        checkpoint: dict[str, ExperimentRun],
    ) -> ExperimentRun:
        """Run one arm of one case, or restore it from the checkpoint."""
        identifier = run_id(plan.id, str(kind), index)
        existing = checkpoint.get(identifier)
        if existing is not None:
            return existing

        started = time.perf_counter()
        try:
            output = model.predict(features)
        except AriadneError as exc:
            # A typed Ariadne error already carries its own retryability. Re-wrapping a
            # non-retryable one as TargetModelError (which IS retryable) would tell the
            # worker to try again at exactly the moment retrying is guaranteed to fail --
            # a budget exhaustion, for instance, is caused by calls, so retrying spends
            # more money to reach the identical outcome. Let those through untouched and
            # only relabel genuinely transient failures.
            if not exc.retryable:
                raise
            raise TargetModelError(
                f"target model failed on {kind} case {index}: {exc}"
            ) from exc
        except Exception as exc:  # an untyped remote failure is assumed transient
            raise TargetModelError(
                f"target model failed on {kind} case {index}: {exc}"
            ) from exc
        duration_ms = (time.perf_counter() - started) * 1000.0

        run = ExperimentRun(
            id=identifier,
            experiment_id=plan.id,
            kind=kind,
            index=index,
            scope=plan.scope,
            features={k: round(float(v), 9) for k, v in features.items()},
            score=output.score,
            decision=output.decision,
            model_explanation=output.explanation,
            input_hash=sha256_hex(features),
            output_hash=sha256_hex(output.as_record()),
            executed_at=self._clock.now(),
            duration_ms=round(duration_ms, 6),
        )
        self._runs.record_run(run)
        return run

    def _measure_instability(
        self, model: TargetModel, plan: ExperimentPlan, cases: list[FixtureCase]
    ) -> float:
        """Ask the model the same question twice and see whether it agrees with itself.

        These probes are intentionally excluded from the evidence sample: they exist to
        qualify the measurement, not to contribute to it.
        """
        replicates: list[tuple[float, float]] = []
        for case in cases[:REPLICATE_PROBES]:
            try:
                first = model.predict(dict(case.features)).score
                second = model.predict(dict(case.features)).score
            except Exception as exc:
                raise TargetModelError(f"instability probe failed: {exc}") from exc
            replicates.append((first, second))
        return instability(replicates)

    # -- evidence ----------------------------------------------------------------------

    def _assemble_evidence(
        self,
        *,
        plan: ExperimentPlan,
        claim: Claim,
        baseline_runs: list[ExperimentRun],
        intervention_runs: list[ExperimentRun],
        control_runs: list[ExperimentRun],
        validity: ValidityReport,
        model_instability: float,
    ) -> Evidence:
        baseline = summarize(RunKind.BASELINE, baseline_runs)
        intervention = summarize(RunKind.INTERVENTION, intervention_runs)
        control: RunSummary | None = (
            summarize(RunKind.CONTROL, control_runs) if control_runs else None
        )

        deltas = paired_deltas(baseline.scores, intervention.scores)
        control_effect = (
            effect_size(paired_deltas(baseline.scores, control.scores)) if control else None
        )
        interval = bootstrap_ci(deltas, seed=plan.seed)

        all_runs = [*baseline_runs, *intervention_runs, *control_runs]
        input_hashes = [run.input_hash for run in all_runs]
        output_hashes = [run.output_hash for run in all_runs]

        evidence_fingerprint = sha256_hex(
            {
                "experiment_id": plan.id,
                "claim_id": claim.id,
                "scope": plan.scope,
                "protocol_version": plan.protocol_version,
                "seed": plan.seed,
                "input_hashes": input_hashes,
                "output_hashes": output_hashes,
            }
        )

        return Evidence(
            id=evidence_id(plan.id, evidence_fingerprint),
            experiment_id=plan.id,
            claim_id=claim.id,
            claim_family_id=claim.claim_family_id,
            scope=plan.scope,
            protocol_version=plan.protocol_version,
            baseline=baseline,
            intervention=intervention,
            control=control,
            effect_size=effect_size(deltas),
            effect_ci=(interval.low, interval.high) if interval else None,
            control_effect_size=control_effect,
            reproducibility=reproducibility(
                deltas, plan.expected_direction, plan.min_effect_threshold
            ),
            validity_score=validity.score,
            instability=model_instability,
            run_ids=[run.id for run in all_runs],
            input_hashes=input_hashes,
            output_hashes=output_hashes,
            evidence_hash=evidence_fingerprint,
            created_at=self._clock.now(),
        )
