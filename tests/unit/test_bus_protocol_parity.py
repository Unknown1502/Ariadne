"""Both event buses must implement the whole protocol, with matching sync/async shape.

The `EventBus` protocol originally declared three methods while callers used ten. Both
implementations happened to provide all ten, so nothing broke - but nothing was checking
either. A third transport could have satisfied the three-method protocol, type-checked
clean, and raised AttributeError the first time a duplicate event arrived.

The sync/async check matters just as much and is easier to get wrong: an implementation
that made `stop()` synchronous would satisfy a naive `hasattr` check while every `await
bus.stop()` in the codebase raised at runtime.
"""

from __future__ import annotations

import inspect

import pytest

from backend.events.bus import EventBus, LocalEventBus, PubSubEventBus

IMPLEMENTATIONS = [LocalEventBus, PubSubEventBus]
PROTOCOL_MEMBERS = sorted(
    name
    for name in dir(EventBus)
    if not name.startswith("_") and name not in {"mro"}
)


class TestProtocolIsComplete:
    def test_the_protocol_covers_what_callers_actually_use(self) -> None:
        # Guards against the protocol shrinking back to publish/subscribe/drain.
        for required in (
            "publish", "subscribe", "drain", "start", "stop", "snapshot",
            "publish_duplicate", "note_duplicate_suppressed",
            "dead_letters", "published_events",
        ):
            assert required in PROTOCOL_MEMBERS, f"{required} dropped from the EventBus protocol"


@pytest.mark.parametrize("implementation", IMPLEMENTATIONS, ids=lambda c: c.__name__)
class TestEveryImplementationConforms:
    def test_every_protocol_member_exists(self, implementation: type) -> None:
        missing = [name for name in PROTOCOL_MEMBERS if not hasattr(implementation, name)]
        assert not missing, f"{implementation.__name__} is missing {missing}"

    def test_sync_and_async_shape_matches_the_protocol(self, implementation: type) -> None:
        # A sync `stop()` would pass a hasattr check and fail on every `await bus.stop()`.
        mismatches: list[str] = []
        for name in PROTOCOL_MEMBERS:
            expected = inspect.getattr_static(EventBus, name)
            actual = inspect.getattr_static(implementation, name)
            if isinstance(expected, property) or isinstance(actual, property):
                if isinstance(expected, property) != isinstance(actual, property):
                    mismatches.append(f"{name}: property/method mismatch")
                continue
            if inspect.iscoroutinefunction(expected) != inspect.iscoroutinefunction(actual):
                mismatches.append(
                    f"{name}: protocol is "
                    f"{'async' if inspect.iscoroutinefunction(expected) else 'sync'}, "
                    f"implementation is "
                    f"{'async' if inspect.iscoroutinefunction(actual) else 'sync'}"
                )
        assert not mismatches, f"{implementation.__name__}: {mismatches}"


class TestImplementationsAgreeWithEachOther:
    def test_pubsub_provides_everything_local_does(self) -> None:
        """The direction that can actually break something.

        Development happens against `LocalEventBus`, so a method added there is the one a
        caller starts depending on. If Pub/Sub lacks it, flipping `EVENT_BUS=pubsub` turns
        that call into a production AttributeError.

        The reverse is deliberately allowed: `PubSubEventBus` carries transport-specific
        members (`run_subscriber` for the pull loop, `max_attempts` for the dead-letter
        policy) that have no meaning in-process. Demanding symmetry there would force
        meaningless stubs onto the local bus.
        """
        local = {n for n in dir(LocalEventBus) if not n.startswith("_")}
        pubsub = {n for n in dir(PubSubEventBus) if not n.startswith("_")}
        assert not (local - pubsub), (
            f"LocalEventBus has public members PubSubEventBus lacks: {sorted(local - pubsub)}. "
            f"Callers written against the local bus would break under EVENT_BUS=pubsub."
        )

    def test_pubsub_extras_are_transport_specific_and_declared(self) -> None:
        # Keeps the exemption honest: new asymmetry has to be named deliberately.
        local = {n for n in dir(LocalEventBus) if not n.startswith("_")}
        pubsub = {n for n in dir(PubSubEventBus) if not n.startswith("_")}
        declared = {"run_subscriber", "max_attempts"}
        undeclared = sorted((pubsub - local) - declared)
        assert not undeclared, f"undeclared Pub/Sub-only members: {undeclared}"
