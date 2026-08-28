"""Policy: debt weights and governance thresholds.

These numbers are choices, not discoveries. Explanation Debt is an operational
prioritization signal, and its weights encode an opinion about which explanation problems
deserve attention first. Nothing here is a scientific quantity, and the code says so in the
one place it matters: every debt snapshot and every Governor decision records the
``policy_version`` it was computed under.

That versioning is the whole point. Change a weight and the version must change with it, so
a debt score from March can never be silently compared against one computed under different
rules. A score whose definition drifts is worse than no score, because it still looks
comparable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from backend.core.errors import ValidationError
from backend.core.hashing import short_hash


@dataclass(frozen=True, slots=True)
class DebtWeights:
    """Points each component can contribute to a 0-100 debt score.

    The weights come from ``docs/04-explanation-debt.md``. They must total 100, so the score
    has a fixed ceiling and a component's share is directly readable.
    """

    stale_evidence: float = 25.0
    contradictions: float = 25.0
    inconclusive: float = 20.0
    version_inconsistency: float = 15.0
    distribution_sensitivity: float = 15.0

    def total(self) -> float:
        return (
            self.stale_evidence
            + self.contradictions
            + self.inconclusive
            + self.version_inconsistency
            + self.distribution_sensitivity
        )

    def validate(self) -> None:
        if abs(self.total() - 100.0) > 1e-9:
            raise ValidationError(
                f"debt weights must total 100 so the score has a fixed ceiling; "
                f"got {self.total()}"
            )
        for name in (
            "stale_evidence",
            "contradictions",
            "inconclusive",
            "version_inconsistency",
            "distribution_sensitivity",
        ):
            if getattr(self, name) < 0:
                raise ValidationError(f"debt weight {name!r} cannot be negative")

    def as_dict(self) -> dict[str, float]:
        return {
            "stale_evidence": self.stale_evidence,
            "contradictions": self.contradictions,
            "inconclusive": self.inconclusive,
            "version_inconsistency": self.version_inconsistency,
            "distribution_sensitivity": self.distribution_sensitivity,
        }


@dataclass(frozen=True, slots=True)
class GovernorThresholds:
    """Where the Governor changes its mind.

    Ordered from least to most disruptive. ``pause_workflow`` is deliberately high and
    additionally gated on human approval: pausing a workflow is the one action with real
    operational blast radius, and no autonomous system should reach it on a score alone.
    """

    schedule_reaudit_debt: float = 25.0
    increase_priority_debt: float = 40.0
    human_review_debt: float = 65.0
    pause_workflow_debt: float = 85.0
    repeated_contradiction_count: int = 2
    stale_days: int = 90

    def validate(self) -> None:
        ladder = [
            self.schedule_reaudit_debt,
            self.increase_priority_debt,
            self.human_review_debt,
            self.pause_workflow_debt,
        ]
        if ladder != sorted(ladder):
            raise ValidationError(
                f"governor thresholds must escalate monotonically, got {ladder}"
            )
        if not self.schedule_reaudit_debt >= 0 and self.pause_workflow_debt <= 100:
            raise ValidationError("governor thresholds must lie within [0, 100]")


@dataclass(frozen=True, slots=True)
class Policy:
    """A complete, versioned policy. Immutable once created."""

    version: str = "1.0.0"
    weights: DebtWeights = field(default_factory=DebtWeights)
    thresholds: GovernorThresholds = field(default_factory=GovernorThresholds)
    description: str = "Default Ariadne policy."

    def __post_init__(self) -> None:
        self.weights.validate()
        self.thresholds.validate()

    def fingerprint(self) -> str:
        """Hash of the actual numbers.

        Guards against the failure this whole module exists to prevent: someone edits a
        weight and forgets to bump the version. The fingerprint changes regardless, so a
        test can catch the mismatch.
        """
        return short_hash(
            {
                "version": self.version,
                "weights": self.weights.as_dict(),
                "thresholds": {
                    "schedule_reaudit_debt": self.thresholds.schedule_reaudit_debt,
                    "increase_priority_debt": self.thresholds.increase_priority_debt,
                    "human_review_debt": self.thresholds.human_review_debt,
                    "pause_workflow_debt": self.thresholds.pause_workflow_debt,
                    "repeated_contradiction_count": (
                        self.thresholds.repeated_contradiction_count
                    ),
                    "stale_days": self.thresholds.stale_days,
                },
            }
        )

    def with_weights(self, version: str, weights: DebtWeights) -> Policy:
        """Derive a new policy with different weights.

        Requires a new version string and refuses to reuse the current one. Reweighting
        without re-versioning would make two incomparable scores look comparable, which is
        exactly the mistake the API is shaped to prevent.
        """
        if version == self.version:
            raise ValidationError(
                f"changing weights requires a new policy version; {version!r} is already in use"
            )
        return replace(self, version=version, weights=weights)


DEFAULT_POLICY = Policy()
"""The policy the system runs under unless a caller supplies another."""
