"""The investigation state machine.

An investigation is a long-running, crash-interruptible process. The machine exists so
that a resumed worker cannot skip a step, repeat a side effect, or reach a verdict without
having executed an experiment first. Every transition is checked; there is no code path
that writes a state directly.

The graph is the one in ``docs/11-state-machine.md``. The doc branches VERIFICATION into
SUPPORTED / CONTRADICTED / INCONCLUSIVE, but those are *verdict values*, not process
states: all three continue to LINEAGE_UPDATED, and collapsing them keeps the verdict in
exactly one place (the Verdict record) rather than duplicating it in the process state.
"""

from __future__ import annotations

from backend.core.enums import InvestigationState as S
from backend.core.errors import InvalidStateTransition

TRANSITIONS: dict[S, frozenset[S]] = {
    S.CREATED: frozenset({S.INGESTING, S.FAILED, S.QUARANTINED}),
    S.INGESTING: frozenset({S.CLAIM_EXTRACTED, S.FAILED, S.QUARANTINED}),
    S.CLAIM_EXTRACTED: frozenset({S.PROBE_PLANNED, S.FAILED, S.QUARANTINED}),
    S.PROBE_PLANNED: frozenset({S.INTERVENTION_VALIDATED, S.FAILED, S.QUARANTINED}),
    # A rejected intervention is not a crash: it produces an INCONCLUSIVE verdict, so the
    # path back to VERIFICATION is legal and deliberate.
    S.INTERVENTION_VALIDATED: frozenset({S.EXPERIMENT_RUNNING, S.VERIFICATION, S.FAILED}),
    S.EXPERIMENT_RUNNING: frozenset({S.VERIFICATION, S.FAILED}),
    S.VERIFICATION: frozenset({S.LINEAGE_UPDATED, S.FAILED}),
    S.LINEAGE_UPDATED: frozenset({S.DEBT_RECALCULATED, S.FAILED}),
    S.DEBT_RECALCULATED: frozenset({S.GOVERNOR_ACTION, S.FAILED}),
    S.GOVERNOR_ACTION: frozenset({S.COMPLETE, S.REVIEW, S.FAILED}),
    S.REVIEW: frozenset({S.COMPLETE, S.FAILED}),
    S.COMPLETE: frozenset(),
    S.FAILED: frozenset(),
    S.QUARANTINED: frozenset(),
}

TERMINAL_STATES: frozenset[S] = frozenset({S.COMPLETE, S.FAILED, S.QUARANTINED})

ORDERED_STATES: tuple[S, ...] = (
    S.CREATED,
    S.INGESTING,
    S.CLAIM_EXTRACTED,
    S.PROBE_PLANNED,
    S.INTERVENTION_VALIDATED,
    S.EXPERIMENT_RUNNING,
    S.VERIFICATION,
    S.LINEAGE_UPDATED,
    S.DEBT_RECALCULATED,
    S.GOVERNOR_ACTION,
    S.COMPLETE,
)
"""Happy-path order. Used to answer 'has this investigation already passed step X?'
when a worker resumes after a crash."""


def can_transition(current: S, target: S) -> bool:
    """True when moving from `current` to `target` is permitted."""
    return target in TRANSITIONS.get(current, frozenset())


def assert_transition(current: S, target: S) -> None:
    """Raise unless the transition is legal.

    Called on every state change. This is the single choke point that makes 'the worker
    jumped straight to a verdict' an impossible bug rather than a code-review question.
    """
    if current is target:
        raise InvalidStateTransition(
            f"{current} -> {target} is a no-op; a re-delivered event must be handled by "
            f"idempotency, not by re-entering the same state"
        )
    if not can_transition(current, target):
        allowed = sorted(s.value for s in TRANSITIONS.get(current, frozenset()))
        raise InvalidStateTransition(f"{current} -> {target} is not allowed; allowed: {allowed}")


def is_terminal(state: S) -> bool:
    return state in TERMINAL_STATES


def has_completed(state: S, step: S) -> bool:
    """True when `state` is at or past `step` on the happy path.

    A resumed worker uses this to skip work it already did, without needing to consult the
    side-effect store for every step.
    """
    if state in TERMINAL_STATES:
        return True
    if state not in ORDERED_STATES or step not in ORDERED_STATES:
        return False
    return ORDERED_STATES.index(state) >= ORDERED_STATES.index(step)


def next_state(current: S) -> S:
    """The next state on the happy path.

    Raises for terminal states, so a caller cannot loop forever on COMPLETE.
    """
    if current in TERMINAL_STATES:
        raise InvalidStateTransition(f"{current} is terminal and has no successor")
    if current is S.REVIEW:
        return S.COMPLETE
    index = ORDERED_STATES.index(current)
    return ORDERED_STATES[index + 1]


def unreachable_states() -> set[S]:
    """States no transition leads to. A non-empty result (besides CREATED) is a graph bug.

    Exposed so the test suite can assert the machine stays connected as it evolves, rather
    than relying on someone noticing a typo in the transition table.
    """
    reachable: set[S] = set()
    for targets in TRANSITIONS.values():
        reachable |= set(targets)
    return {s for s in S if s not in reachable and s is not S.CREATED}
