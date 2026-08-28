"""The Synthetic Triage Decision Laboratory.

SYNTHETIC SYSTEM - NOT A MEDICAL DEVICE. Every model here is a hand-written arithmetic
formula over three invented features. Nothing in this module was trained, nothing is
derived from real patients, and no output has clinical meaning. The triage framing exists
so the demo has a person in it; the science is about explanation faithfulness, not triage.

The laboratory exists because Ariadne needs *known* ground truth. If the target model were
a real black box, there would be no way to check whether the verifier's verdicts are
correct - and an explanation-auditing system whose own accuracy is unmeasurable is not
worth much. Here, the formula is printed on the page, so what a faithful explanation would
be is not a matter of opinion.

Every version ships the same explanation string:

    "Urgency marker was the primary driver."

That is the experiment. The explanation stays fixed while the model underneath it changes,
so the question "is this explanation still true?" has four different correct answers.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from backend.core.errors import ValidationError
from backend.experiment_engine.distributions import (
    FEATURE_NAMES,
    get_distribution,
    validate_features,
)

MODEL_ID: Final[str] = "synthetic-triage"
STANDING_EXPLANATION: Final[str] = "Urgency marker was the primary driver."
HIGH_PRIORITY_THRESHOLD: Final[float] = 0.60
SCORE_PRECISION: Final[int] = 9

SYNTHETIC_DISCLAIMER: Final[str] = (
    "Synthetic Triage Decision Laboratory. Formula-defined model, invented features, "
    "no clinical validity."
)


@dataclass(frozen=True, slots=True)
class TargetOutput:
    """One decision from the target model."""

    decision: str
    score: float
    explanation: str
    model_id: str
    model_version: str
    distribution_version: str

    def as_record(self) -> dict[str, object]:
        """The canonical, hashable form written into the evidence ledger."""
        return {
            "decision": self.decision,
            "score": self.score,
            "explanation": self.explanation,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "distribution_version": self.distribution_version,
        }


@runtime_checkable
class TargetModel(Protocol):
    """The interface the experiment engine is allowed to know about.

    Deliberately narrow: the engine can ask for a prediction and read version metadata, and
    that is all. It cannot inspect coefficients. If it could, Ariadne would be checking the
    formula rather than the behavior, and the whole result would only apply to models whose
    internals you already have.
    """

    model_id: str
    version: str
    distribution_version: str

    def predict(self, features: dict[str, float]) -> TargetOutput: ...


@dataclass(frozen=True, slots=True)
class VersionSpec:
    """The full, public definition of one model version.

    ``ground_truth_note`` records what a correct auditor *should* conclude and why. It is
    documentation and test-fixture material only - no runtime code reads it, so it cannot
    leak into a verdict.
    """

    version: str
    weight_urgency: float
    weight_signal_b: float
    weight_signal_c: float
    weight_interaction: float
    noise_scale: float
    description: str
    ground_truth_note: str


VERSION_SPECS: Final[dict[str, VersionSpec]] = {
    # Urgency has a small weight while signal_c dominates. The standing explanation is
    # simply wrong about which feature drives the decision.
    "1.0.0": VersionSpec(
        version="1.0.0",
        weight_urgency=0.20,
        weight_signal_b=0.05,
        weight_signal_c=0.75,
        weight_interaction=0.0,
        noise_scale=0.0,
        description="signal_c dominates; the urgency explanation is unfaithful.",
        ground_truth_note=(
            "CONTRADICTED: neutralizing urgency moves the score by at most 0.09 across the "
            "baseline distribution, below the 0.10 effect threshold, while neutralizing "
            "signal_c moves it far more."
        ),
    ),
    # Urgency genuinely dominates. The same sentence is now a faithful description.
    "2.0.0": VersionSpec(
        version="2.0.0",
        weight_urgency=0.80,
        weight_signal_b=0.05,
        weight_signal_c=0.15,
        weight_interaction=0.0,
        noise_scale=0.0,
        description="urgency dominates; the explanation is faithful.",
        ground_truth_note=(
            "SUPPORTED: neutralizing urgency lowers the score by ~0.22 on average, "
            "reproducibly and by more than the control."
        ),
    ),
    # Two comparable weights plus a rough response surface. Individual cases disagree with
    # each other, which is what an honest auditor should refuse to call either way.
    "3.0.0": VersionSpec(
        version="3.0.0",
        weight_urgency=0.50,
        weight_signal_b=0.05,
        weight_signal_c=0.45,
        weight_interaction=0.0,
        noise_scale=0.10,
        description="near-equal weights with a noisy response surface.",
        ground_truth_note=(
            "INCONCLUSIVE: the effect clears the threshold on roughly 60% of cases - "
            "neither reproducibly present nor reproducibly absent."
        ),
    ),
    # Urgency only matters in combination with signal_c. Its main effect is weak, so the
    # claim that it is *the primary driver* fails again, for a new reason.
    "4.0.0": VersionSpec(
        version="4.0.0",
        weight_urgency=0.10,
        weight_signal_b=0.0,
        weight_signal_c=0.70,
        weight_interaction=0.15,
        noise_scale=0.0,
        description="urgency acts only through an interaction with signal_c.",
        ground_truth_note=(
            "CONTRADICTED: the urgency main effect stays under the threshold on nearly "
            "every case because urgency only acts jointly with signal_c."
        ),
    ),
}

KNOWN_VERSIONS: Final[tuple[str, ...]] = tuple(sorted(VERSION_SPECS))


class SyntheticTriageModel:
    """A deterministic, formula-defined target model.

    ``predict`` is a pure function of (features, version, distribution). The same input
    always produces the same output, on any machine, forever. Version 3 adds a perturbation
    but derives it by seeding a PRNG with the feature vector itself, so it stays pure: the
    model has a *rough* response surface, not a random one. Genuine run-to-run instability
    is a different failure, and ``UnstableTriageModel`` exists to test that the verifier
    catches it.
    """

    def __init__(self, version: str, distribution_version: str = "baseline_2024.1") -> None:
        if version not in VERSION_SPECS:
            raise ValidationError(
                f"unknown model version {version!r}; the laboratory defines "
                f"{list(KNOWN_VERSIONS)}"
            )
        get_distribution(distribution_version)  # fail fast on an unknown distribution
        self.model_id = MODEL_ID
        self.version = version
        self.distribution_version = distribution_version
        self.spec = VERSION_SPECS[version]

    def predict(self, features: dict[str, float]) -> TargetOutput:
        validate_features(features)
        urgency = float(features["urgency_marker"])
        signal_b = float(features["signal_b"])
        signal_c = float(features["signal_c"])
        spec = self.spec

        score = (
            spec.weight_urgency * urgency
            + spec.weight_signal_b * signal_b
            + spec.weight_signal_c * signal_c
            + spec.weight_interaction * urgency * signal_c
        )
        if spec.noise_scale:
            score += self._surface_perturbation(features)
        score = round(min(1.0, max(0.0, score)), SCORE_PRECISION)

        return TargetOutput(
            decision=(
                "HIGH_PRIORITY" if score >= HIGH_PRIORITY_THRESHOLD else "STANDARD_PRIORITY"
            ),
            score=score,
            explanation=STANDING_EXPLANATION,
            model_id=self.model_id,
            model_version=self.version,
            distribution_version=self.distribution_version,
        )

    def _surface_perturbation(self, features: dict[str, float]) -> float:
        """A reproducible per-input offset, seeded by the input itself.

        Keyed on the rounded feature vector so floating-point noise in the caller cannot
        change which offset a case gets.
        """
        key = ":".join(f"{name}={features[name]:.9f}" for name in FEATURE_NAMES)
        rng = random.Random(f"{self.model_id}:{self.version}:{key}")
        return rng.gauss(0.0, self.spec.noise_scale)

    def __repr__(self) -> str:
        return (
            f"SyntheticTriageModel(version={self.version!r}, "
            f"distribution={self.distribution_version!r})"
        )


class UnstableTriageModel:
    """A target model that returns a different score every call. Test double only.

    Ariadne must not report a verdict about a model whose behavior it cannot pin down. This
    class exists so that requirement is proven by a test rather than assumed - it is never
    registered as a real version and never reachable from the runtime.
    """

    def __init__(self, version: str = "1.0.0", jitter: float = 0.30) -> None:
        self.model_id = MODEL_ID
        self.version = version
        self.distribution_version = "baseline_2024.1"
        self._base = SyntheticTriageModel(version)
        self._jitter = jitter
        self._calls = 0

    def predict(self, features: dict[str, float]) -> TargetOutput:
        out = self._base.predict(features)
        self._calls += 1
        rng = random.Random(f"unstable:{self._calls}")
        score = round(min(1.0, max(0.0, out.score + rng.gauss(0.0, self._jitter))), 9)
        return TargetOutput(
            decision=(
                "HIGH_PRIORITY" if score >= HIGH_PRIORITY_THRESHOLD else "STANDARD_PRIORITY"
            ),
            score=score,
            explanation=out.explanation,
            model_id=out.model_id,
            model_version=out.model_version,
            distribution_version=out.distribution_version,
        )


class FailingTargetModel:
    """A target model that always raises. Test double only, for the failure path."""

    def __init__(self, version: str = "1.0.0", message: str = "target model unavailable") -> None:
        self.model_id = MODEL_ID
        self.version = version
        self.distribution_version = "baseline_2024.1"
        self._message = message

    def predict(self, features: dict[str, float]) -> TargetOutput:
        raise RuntimeError(self._message)


def get_target_model(
    version: str, distribution_version: str = "baseline_2024.1"
) -> SyntheticTriageModel:
    """Build the target model for a version. Unknown versions fail closed."""
    return SyntheticTriageModel(version, distribution_version)


def describe_version(version: str) -> dict[str, object]:
    """Public description of a version, for the console and the benchmark report."""
    spec = VERSION_SPECS.get(version)
    if spec is None:
        raise ValidationError(f"unknown model version {version!r}")
    return {
        "model_id": MODEL_ID,
        "version": spec.version,
        "formula": formula_text(spec),
        "noise_scale": spec.noise_scale,
        "description": spec.description,
        "standing_explanation": STANDING_EXPLANATION,
        "disclaimer": SYNTHETIC_DISCLAIMER,
    }


def formula_text(spec: VersionSpec) -> str:
    """Human-readable formula. Published so a judge can check the ground truth by hand."""
    parts = []
    if spec.weight_urgency:
        parts.append(f"{spec.weight_urgency:g}*urgency_marker")
    if spec.weight_signal_b:
        parts.append(f"{spec.weight_signal_b:g}*signal_b")
    if spec.weight_signal_c:
        parts.append(f"{spec.weight_signal_c:g}*signal_c")
    if spec.weight_interaction:
        parts.append(f"{spec.weight_interaction:g}*urgency_marker*signal_c")
    if spec.noise_scale:
        parts.append(f"N(0,{spec.noise_scale:g}) keyed by the input vector")
    return "score = " + " + ".join(parts)
