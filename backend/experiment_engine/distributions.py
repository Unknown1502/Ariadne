"""Feature space, data distributions, and deterministic fixture sets.

SYNTHETIC LABORATORY. The features below are named after a triage narrative so the demo
reads as something a person would care about. They are not clinical variables, they are not
derived from patient data, and nothing here is a medical device.

Two ideas live in this module:

  - A **distribution** is the joint range the target model's inputs are drawn from.
    "Distribution shift" is a real, declared change to these ranges, not a metaphor.
  - A **fixture set** is a reproducible list of cases sampled from a distribution. Ground
    truth comes from these fixtures plus the model formulas, never from a language model.

Everything is seeded. Regenerating a fixture set on another machine, in another year, gives
byte-identical cases.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Final

from backend.core.errors import ValidationError

# --------------------------------------------------------------------------------------
# Feature space
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """One input feature, with the range that makes it realistic.

    ``neutral_value`` is what "neutralize this feature" means operationally. Defining it
    here, once, keeps the meaning of an intervention out of the agent's hands: the
    Experimenter can choose *whether* to neutralize, never what neutralizing is.
    """

    name: str
    minimum: float
    maximum: float
    neutral_value: float
    description: str

    def contains(self, value: float) -> bool:
        return self.minimum <= value <= self.maximum


FEATURES: Final[tuple[FeatureSpec, ...]] = (
    FeatureSpec(
        name="urgency_marker",
        minimum=0.0,
        maximum=1.0,
        neutral_value=0.5,
        description="Synthetic intake urgency indicator claimed by the model explanation.",
    ),
    FeatureSpec(
        name="signal_b",
        minimum=0.0,
        maximum=1.0,
        neutral_value=0.5,
        description="Synthetic secondary signal with small weight in every model version.",
    ),
    FeatureSpec(
        name="signal_c",
        minimum=0.0,
        maximum=1.0,
        neutral_value=0.5,
        description="Synthetic primary competitor signal. The control variable of the demo.",
    ),
)

FEATURE_NAMES: Final[tuple[str, ...]] = tuple(f.name for f in FEATURES)
FEATURE_INDEX: Final[dict[str, FeatureSpec]] = {f.name: f for f in FEATURES}


def feature_spec(name: str) -> FeatureSpec:
    try:
        return FEATURE_INDEX[name]
    except KeyError:
        raise ValidationError(
            f"unknown feature {name!r}; the laboratory defines {list(FEATURE_NAMES)}"
        ) from None


def neutral_value(name: str) -> float:
    return feature_spec(name).neutral_value


def validate_features(features: dict[str, float]) -> None:
    """Reject a feature vector the laboratory cannot evaluate.

    Strict on purpose: an unknown or out-of-range feature means the caller and the model
    disagree about the input space, and a score computed under that disagreement would be
    meaningless while still looking like a number.
    """
    missing = [name for name in FEATURE_NAMES if name not in features]
    if missing:
        raise ValidationError(f"feature vector is missing {missing}")
    unknown = [name for name in features if name not in FEATURE_INDEX]
    if unknown:
        raise ValidationError(f"feature vector contains unknown features {unknown}")
    for name, value in features.items():
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise ValidationError(f"feature {name!r} must be numeric, got {type(value).__name__}")
        spec = FEATURE_INDEX[name]
        if not spec.contains(float(value)):
            raise ValidationError(
                f"feature {name!r}={value} is outside its realistic range "
                f"[{spec.minimum}, {spec.maximum}]"
            )


# --------------------------------------------------------------------------------------
# Distributions
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Distribution:
    """A declared input distribution. Changing one is what DISTRIBUTION_CHANGED reports."""

    version: str
    description: str
    ranges: dict[str, tuple[float, float]]
    seed: int

    def sample(self, index: int) -> dict[str, float]:
        """Deterministically sample case number `index`.

        Seeded per case rather than per sequence, so case 7 is the same case whether you
        generated 8 cases or 800 - fixture sets of different sizes stay comparable.
        """
        rng = random.Random(f"{self.version}:{self.seed}:{index}")
        case: dict[str, float] = {}
        for name in FEATURE_NAMES:
            low, high = self.ranges[name]
            case[name] = round(rng.uniform(low, high), 6)
        return case

    def midpoint(self, name: str) -> float:
        low, high = self.ranges[name]
        return (low + high) / 2.0


DISTRIBUTIONS: Final[dict[str, Distribution]] = {
    "baseline_2024.1": Distribution(
        version="baseline_2024.1",
        description=(
            "Original synthetic intake population. Urgency runs high and sits well away "
            "from its neutral value, so a neutralizing intervention is a genuinely large "
            "perturbation on every case."
        ),
        ranges={
            "urgency_marker": (0.65, 0.95),
            "signal_b": (0.10, 0.50),
            "signal_c": (0.50, 0.90),
        },
        seed=20240101,
    ),
    "shifted_2025.2": Distribution(
        version="shifted_2025.2",
        description=(
            "Shifted intake population. Urgency now clusters near its neutral value, so a "
            "neutralizing intervention barely moves the input and the same claim becomes "
            "much harder to test."
        ),
        ranges={
            "urgency_marker": (0.42, 0.58),
            "signal_b": (0.10, 0.50),
            "signal_c": (0.55, 0.95),
        },
        seed=20250202,
    ),
}


def get_distribution(version: str) -> Distribution:
    try:
        return DISTRIBUTIONS[version]
    except KeyError:
        raise ValidationError(
            f"unknown distribution_version {version!r}; known: {sorted(DISTRIBUTIONS)}"
        ) from None


# --------------------------------------------------------------------------------------
# Fixture sets
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FixtureCase:
    case_id: str
    index: int
    features: dict[str, float]
    distribution_version: str


@dataclass(frozen=True, slots=True)
class FixtureSet:
    """A named, reproducible collection of cases. The unit an experiment runs over."""

    name: str
    distribution_version: str
    size: int

    def cases(self, count: int | None = None) -> list[FixtureCase]:
        distribution = get_distribution(self.distribution_version)
        n = self.size if count is None else count
        if n < 1:
            raise ValidationError(f"a fixture set needs at least one case, asked for {n}")
        if n > self.size:
            raise ValidationError(
                f"fixture set {self.name!r} holds {self.size} cases, asked for {n}; "
                f"silently generating extra cases would change what the set means"
            )
        return [
            FixtureCase(
                case_id=f"{self.name}#{i:03d}",
                index=i,
                features=distribution.sample(i),
                distribution_version=self.distribution_version,
            )
            for i in range(n)
        ]


FIXTURE_SETS: Final[dict[str, FixtureSet]] = {
    "triage_baseline_v1": FixtureSet(
        name="triage_baseline_v1", distribution_version="baseline_2024.1", size=64
    ),
    "triage_shifted_v1": FixtureSet(
        name="triage_shifted_v1", distribution_version="shifted_2025.2", size=64
    ),
}

FIXTURE_SET_FOR_DISTRIBUTION: Final[dict[str, str]] = {
    fixtures.distribution_version: name for name, fixtures in FIXTURE_SETS.items()
}


def get_fixture_set(name: str) -> FixtureSet:
    try:
        return FIXTURE_SETS[name]
    except KeyError:
        raise ValidationError(
            f"unknown fixture_set {name!r}; known: {sorted(FIXTURE_SETS)}"
        ) from None


def fixture_set_for(distribution_version: str) -> FixtureSet:
    """The canonical fixture set for a distribution.

    Used when an event names a distribution and the runtime has to pick cases without an
    agent choosing them - the agent must not be able to select the data that flatters it.
    """
    name = FIXTURE_SET_FOR_DISTRIBUTION.get(distribution_version)
    if name is None:
        raise ValidationError(f"no fixture set is registered for {distribution_version!r}")
    return FIXTURE_SETS[name]
