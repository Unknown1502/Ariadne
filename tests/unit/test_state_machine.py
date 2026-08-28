"""State machine tests.

The machine's job is to make two classes of bug impossible rather than unlikely:
reaching a verdict without running an experiment, and a resumed worker silently redoing
or skipping a step.
"""

from __future__ import annotations

import pytest

from backend.core.enums import InvestigationState as S
from backend.core.errors import InvalidStateTransition
from backend.core.state_machine import (
    ORDERED_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    assert_transition,
    can_transition,
    has_completed,
    is_terminal,
    next_state,
    unreachable_states,
)


class TestGraphIntegrity:
    def test_every_state_has_a_transition_entry(self) -> None:
        assert set(TRANSITIONS) == set(S)

    def test_no_state_is_stranded(self) -> None:
        assert unreachable_states() == set()

    def test_terminal_states_have_no_successors(self) -> None:
        for state in TERMINAL_STATES:
            assert TRANSITIONS[state] == frozenset()
            assert is_terminal(state)

    def test_every_non_terminal_state_can_reach_failure(self) -> None:
        # A worker must always have somewhere to put an unrecoverable error.
        for state, targets in TRANSITIONS.items():
            if state not in TERMINAL_STATES:
                assert S.FAILED in targets, f"{state} cannot record a failure"

    def test_happy_path_is_a_valid_walk(self) -> None:
        for current, target in zip(ORDERED_STATES, ORDERED_STATES[1:], strict=False):
            assert can_transition(current, target), f"{current} -> {target} is broken"


class TestTransitions:
    def test_legal_transition_is_allowed(self) -> None:
        assert_transition(S.CREATED, S.INGESTING)

    def test_skipping_the_experiment_is_refused(self) -> None:
        # The central safety property: no verdict without an experiment.
        with pytest.raises(InvalidStateTransition):
            assert_transition(S.CLAIM_EXTRACTED, S.VERIFICATION)
        with pytest.raises(InvalidStateTransition):
            assert_transition(S.CREATED, S.GOVERNOR_ACTION)

    def test_moving_backwards_is_refused(self) -> None:
        with pytest.raises(InvalidStateTransition):
            assert_transition(S.VERIFICATION, S.EXPERIMENT_RUNNING)

    def test_self_transition_is_refused_and_says_why(self) -> None:
        with pytest.raises(InvalidStateTransition, match="idempotency"):
            assert_transition(S.EXPERIMENT_RUNNING, S.EXPERIMENT_RUNNING)

    def test_terminal_states_cannot_be_left(self) -> None:
        for terminal in TERMINAL_STATES:
            with pytest.raises(InvalidStateTransition):
                assert_transition(terminal, S.INGESTING)

    def test_rejected_intervention_short_circuits_to_verification(self) -> None:
        # An invalid plan is INCONCLUSIVE evidence, not a crash.
        assert_transition(S.INTERVENTION_VALIDATED, S.VERIFICATION)

    def test_error_message_lists_what_was_allowed(self) -> None:
        with pytest.raises(InvalidStateTransition, match="allowed:"):
            assert_transition(S.CREATED, S.COMPLETE)


class TestResumption:
    def test_completed_steps_are_recognised(self) -> None:
        assert has_completed(S.VERIFICATION, S.EXPERIMENT_RUNNING)
        assert has_completed(S.VERIFICATION, S.VERIFICATION)
        assert not has_completed(S.CLAIM_EXTRACTED, S.VERIFICATION)

    def test_terminal_states_count_as_having_completed_everything(self) -> None:
        for terminal in TERMINAL_STATES:
            assert has_completed(terminal, S.GOVERNOR_ACTION)

    def test_next_state_walks_the_happy_path(self) -> None:
        assert next_state(S.CREATED) is S.INGESTING
        assert next_state(S.GOVERNOR_ACTION) is S.COMPLETE
        assert next_state(S.REVIEW) is S.COMPLETE

    def test_next_state_refuses_to_advance_a_finished_investigation(self) -> None:
        for terminal in TERMINAL_STATES:
            with pytest.raises(InvalidStateTransition, match="terminal"):
                next_state(terminal)
