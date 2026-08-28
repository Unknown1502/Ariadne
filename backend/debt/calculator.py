"""Explanation Debt.

Debt is a decomposable operational risk score in [0, 100]. It answers one question: given
everything currently known about a model's explanations, how much unresolved explanation
risk is outstanding?

Three properties keep it honest:

  - **Decomposable.** Every component reports its ratio, its weight, and the resulting
    points, plus the claim families that produced it. ``ratio * weight == points`` is
    enforced by the schema, so the arithmetic can be checked rather than trusted.
  - **Deterministic.** The same lineage and the same policy always give the same number.
  - **Not scientific.** Debt is a prioritization signal whose weights are policy choices.
    Every snapshot records its ``policy_version``, and the docs say plainly that this is an
    operational score rather than a measurement of anything in the world.

Note what debt does *not* do: it never changes a verdict. It reads the record and scores it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.core.clock import Clock, SystemClock
from backend.core.enums import LineageRelation, VerdictStatus
from backend.core.ids import DEBT_PREFIX, derive_id
from backend.core.schemas import DebtComponent, DebtSnapshot
from backend.governance.policy import DEFAULT_POLICY, Policy
from backend.lineage.service import LineageService, LineageView


@dataclass(frozen=True, slots=True)
class FamilyAssessment:
    """What one claim family contributes to the score."""

    claim_family_id: str
    current_status: VerdictStatus | None
    is_stale: bool
    has_expired_evidence: bool
    version_inconsistent: bool
    distribution_mismatch: bool


class DebtCalculator:
    """Computes Explanation Debt from lineage."""

    def __init__(
        self,
        lineage: LineageService,
        *,
        policy: Policy = DEFAULT_POLICY,
        clock: Clock | None = None,
    ) -> None:
        self._lineage = lineage
        self._policy = policy
        self._clock = clock or SystemClock()

    def calculate(
        self,
        model_id: str,
        *,
        scope_label: str | None = None,
        at: datetime | None = None,
        previous_total: float | None = None,
        trigger_event_id: str | None = None,
        current_distribution: str | None = None,
    ) -> DebtSnapshot:
        """Score every claim family for a model and produce an immutable snapshot."""
        moment = at or self._clock.now()
        families = self._lineage.families_for_model(model_id)
        assessments = [
            self._assess(family, moment, current_distribution) for family in families
        ]

        components = self._components(assessments)
        total = round(sum(component.points for component in components), 6)

        return DebtSnapshot(
            id=derive_id(DEBT_PREFIX, model_id, moment.isoformat(), total, self._policy.version),
            model_id=model_id,
            scope_label=scope_label or model_id,
            policy_version=self._policy.version,
            components=components,
            total=total,
            previous_total=previous_total,
            computed_at=moment,
            trigger_event_id=trigger_event_id,
        )

    # -- components --------------------------------------------------------------------

    def _components(self, assessments: list[FamilyAssessment]) -> list[DebtComponent]:
        weights = self._policy.weights
        total = len(assessments)

        if total == 0:
            # No claims means no known explanation risk. Reporting zero with the components
            # still present keeps the breakdown's shape stable for consumers.
            return [
                self._component(name, 0.0, weight, "no claim families recorded", [])
                for name, weight in weights.as_dict().items()
            ]

        def ratio(predicate) -> tuple[float, list[str]]:
            matching = [a.claim_family_id for a in assessments if predicate(a)]
            return len(matching) / total, matching

        stale_ratio, stale_ids = ratio(lambda a: a.is_stale)
        contradicted_ratio, contradicted_ids = ratio(
            lambda a: a.current_status is VerdictStatus.CONTRADICTED
        )
        inconclusive_ratio, inconclusive_ids = ratio(
            lambda a: a.current_status is VerdictStatus.INCONCLUSIVE
        )
        version_ratio, version_ids = ratio(lambda a: a.version_inconsistent)
        distribution_ratio, distribution_ids = ratio(
            lambda a: a.has_expired_evidence or a.distribution_mismatch
        )

        return [
            self._component(
                "stale_evidence",
                stale_ratio,
                weights.stale_evidence,
                f"{len(stale_ids)}/{total} families have no current, in-window evidence",
                stale_ids,
            ),
            self._component(
                "contradictions",
                contradicted_ratio,
                weights.contradictions,
                f"{len(contradicted_ids)}/{total} families are currently CONTRADICTED",
                contradicted_ids,
            ),
            self._component(
                "inconclusive",
                inconclusive_ratio,
                weights.inconclusive,
                f"{len(inconclusive_ids)}/{total} families are currently INCONCLUSIVE",
                inconclusive_ids,
            ),
            self._component(
                "version_inconsistency",
                version_ratio,
                weights.version_inconsistency,
                f"{len(version_ids)}/{total} families disagree across model versions",
                version_ids,
            ),
            self._component(
                "distribution_sensitivity",
                distribution_ratio,
                weights.distribution_sensitivity,
                f"{len(distribution_ids)}/{total} families rest on superseded or expired data",
                distribution_ids,
            ),
        ]

    @staticmethod
    def _component(
        name: str, ratio: float, weight: float, detail: str, ids: list[str]
    ) -> DebtComponent:
        bounded = max(0.0, min(1.0, ratio))
        return DebtComponent(
            name=name,
            ratio=round(bounded, 9),
            weight=weight,
            points=round(bounded * weight, 9),
            detail=detail,
            contributing_ids=ids[:200],
        )

    # -- per-family assessment ---------------------------------------------------------

    def _assess(
        self, family: str, moment: datetime, current_distribution: str | None
    ) -> FamilyAssessment:
        view = self._lineage.view(family, at=moment)
        current = view.current

        is_stale = current is None or self._is_stale(current.valid_from, moment)
        distribution_mismatch = bool(
            current is not None
            and current_distribution is not None
            and current.scope.distribution_version != current_distribution
        )

        return FamilyAssessment(
            claim_family_id=family,
            current_status=current.status if current else None,
            is_stale=is_stale,
            has_expired_evidence=view.has_expired_evidence,
            version_inconsistent=self._is_version_inconsistent(view),
            distribution_mismatch=distribution_mismatch,
        )

    def _is_stale(self, valid_from: datetime, moment: datetime) -> bool:
        return (moment - valid_from) > timedelta(days=self._policy.thresholds.stale_days)

    @staticmethod
    def _is_version_inconsistent(view: LineageView) -> bool:
        """True when the same claim reached different verdicts on different versions.

        This is the temporal signal that makes Ariadne more than a test suite: an
        explanation that was contradicted on v1 and supported on v2 is not settled, and the
        disagreement itself is a form of outstanding risk.
        """
        statuses = {
            entry.status
            for entry in view.entries
            if entry.relation is not LineageRelation.EXPIRES
        }
        return len(statuses) > 1


def explain(snapshot: DebtSnapshot) -> str:
    """Render the breakdown the way the docs and the console present it."""
    lines = [f"Explanation Debt: {snapshot.total:.0f} / 100"]
    for component in sorted(snapshot.components, key=lambda c: -c.points):
        if component.points > 0:
            label = component.name.replace("_", " ").capitalize()
            lines.append(f"{label}: +{component.points:.0f}")
    if snapshot.delta is not None:
        lines.append(f"Change since previous snapshot: {snapshot.delta:+.1f}")
    lines.append(f"Policy version: {snapshot.policy_version}")
    return "\n".join(lines)
