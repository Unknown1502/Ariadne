"""The event bus.

Pub/Sub in deployment; an in-process asyncio bus locally. Both provide the same contract,
and the contract is the interesting part: **at-least-once delivery**.

That is not a limitation being worked around, it is the property the system is designed
for. Pub/Sub will redeliver. A worker will occasionally die between doing the work and
acknowledging it. So the bus deliberately makes duplicates *easy* to produce - the local
implementation can even be told to duplicate on purpose - and correctness comes from the
consumer being idempotent rather than from the transport being exactly-once.

Delivery failures escalate the same way in both implementations: bounded retries with
exponential backoff, then the dead-letter topic. An event that cannot be processed is
parked with its error, never silently dropped and never retried forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.core.errors import AriadneError
from backend.events.schemas import AriadneEvent

EventHandler = Callable[[AriadneEvent], Coroutine[Any, Any, None]]
"""Handlers are coroutine functions, not merely awaitables.

The Pub/Sub bus hands them to ``asyncio.run_coroutine_threadsafe``, which needs a
real Coroutine. Annotating the weaker Awaitable let that mismatch pass unnoticed.
"""


@dataclass
class DeadLetter:
    """An event that exhausted its retries, kept with the reason it failed."""

    event: AriadneEvent
    error_code: str
    error_detail: str
    attempts: int


@dataclass
class DeliveryStats:
    """What the bus did. The console's resilience panel reads these."""

    published: int = 0
    delivered: int = 0
    duplicates_suppressed: int = 0
    retried: int = 0
    dead_lettered: int = 0
    failed: int = 0


class EventBus(Protocol):
    """Everything a caller may rely on, regardless of transport.

    This deliberately declares the *whole* surface rather than the three obvious methods.
    An incomplete protocol is a type checker that cannot do its job: callers reach for
    ``start``, ``snapshot``, ``dead_letters``, and ``note_duplicate_suppressed`` too, and
    while both current implementations provide them, nothing forced a third one to. A new
    transport could satisfy a three-method protocol, type-check clean, and then raise
    AttributeError the first time a duplicate arrived in production.

    The Pub/Sub implementation was already written to this full surface so that no caller
    has to branch on which bus it holds. Writing it down here makes that a checked
    requirement instead of a convention someone has to remember.
    """

    async def publish(self, event: AriadneEvent) -> None: ...
    def subscribe(self, handler: EventHandler) -> None: ...
    async def drain(self) -> None: ...

    def start(self) -> None: ...
    async def stop(self) -> None: ...
    def snapshot(self) -> dict[str, Any]: ...

    async def publish_duplicate(self, event: AriadneEvent) -> None:
        """Deliver the same event twice on purpose, to demonstrate idempotency."""
        ...

    def note_duplicate_suppressed(self) -> None:
        """Record that a redelivery was recognised and skipped."""
        ...

    @property
    def dead_letters(self) -> list[DeadLetter]: ...

    @property
    def published_events(self) -> list[AriadneEvent]: ...


class LocalEventBus:
    """In-process asyncio bus with real retry, backoff, and dead-lettering.

    Genuinely asynchronous: publishing returns immediately and the work happens in a
    background task. The demo's "nobody clicked Analyze" claim depends on that being true
    rather than staged - the console publishes an event and the pipeline runs on its own.
    """

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        base_delay: float = 0.01,
        max_delay: float = 1.0,
        duplicate_rate: float = 0.0,
        seed: int = 1234,
    ) -> None:
        self._queue: asyncio.Queue[tuple[AriadneEvent, int]] = asyncio.Queue()
        self._handlers: list[EventHandler] = []
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._duplicate_rate = duplicate_rate
        self._rng = random.Random(seed)
        self._dead_letters: list[DeadLetter] = []
        self._published: list[AriadneEvent] = []
        self.stats = DeliveryStats()
        self._task: asyncio.Task[None] | None = None
        self._running = False

    # -- publishing --------------------------------------------------------------------

    async def publish(self, event: AriadneEvent) -> None:
        self._published.append(event)
        self.stats.published += 1
        await self._queue.put((event, 1))

        if self._duplicate_rate and self._rng.random() < self._duplicate_rate:
            # Deliberate redelivery. The consumer must be unbothered by it.
            await self._queue.put((event, 1))

    async def publish_duplicate(self, event: AriadneEvent) -> None:
        """Redeliver an event on purpose. Used by the demo and the chaos suite."""
        await self._queue.put((event, 1))

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    # -- running -----------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while self._running:
            try:
                event, attempt = await self._queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self._dispatch(event, attempt)
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        """Process everything currently queued, including retries. Used by tests."""
        while not self._queue.empty():
            event, attempt = await self._queue.get()
            try:
                await self._dispatch(event, attempt)
            finally:
                self._queue.task_done()
            await asyncio.sleep(0)

    async def _dispatch(self, event: AriadneEvent, attempt: int) -> None:
        for handler in self._handlers:
            try:
                await handler(event)
                self.stats.delivered += 1
            except Exception as exc:
                await self._handle_failure(event, attempt, exc)

    async def _handle_failure(
        self, event: AriadneEvent, attempt: int, exc: Exception
    ) -> None:
        retryable = getattr(exc, "retryable", True) if isinstance(exc, AriadneError) else True
        self.stats.failed += 1

        if not retryable or attempt >= self._max_attempts:
            self._dead_letters.append(
                DeadLetter(
                    event=event,
                    error_code=type(exc).__name__,
                    error_detail=str(exc)[:1000],
                    attempts=attempt,
                )
            )
            self.stats.dead_lettered += 1
            return

        delay = min(self._max_delay, self._base_delay * (2 ** (attempt - 1)))
        self.stats.retried += 1
        await asyncio.sleep(delay)
        await self._queue.put((event.next_attempt(), attempt + 1))

    # -- inspection --------------------------------------------------------------------

    @property
    def dead_letters(self) -> list[DeadLetter]:
        return list(self._dead_letters)

    @property
    def published_events(self) -> list[AriadneEvent]:
        return list(self._published)

    def note_duplicate_suppressed(self) -> None:
        """Called by the worker when idempotency stopped a redelivery doing work twice."""
        self.stats.duplicates_suppressed += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "published": self.stats.published,
            "delivered": self.stats.delivered,
            "duplicates_suppressed": self.stats.duplicates_suppressed,
            "retried": self.stats.retried,
            "dead_lettered": self.stats.dead_lettered,
            "failed": self.stats.failed,
            "queued": self._queue.qsize(),
        }


@dataclass
class PubSubEventBus:
    """Google Cloud Pub/Sub adapter.

    Same contract as the local bus. Pub/Sub already provides at-least-once delivery,
    retries, and dead-lettering, so this maps onto them rather than reimplementing them:
    an unacked message is redelivered, and the subscription's dead-letter policy parks
    messages that exceed their delivery attempts.
    """

    project_id: str
    topic: str
    subscription: str
    dead_letter_topic: str
    max_attempts: int = 5
    _handlers: list[EventHandler] = field(default_factory=list)
    _subscription_future: Any = None
    stats: DeliveryStats = field(default_factory=DeliveryStats)

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    # -- lifecycle, matching LocalEventBus so callers need no branching -----------------

    def start(self) -> None:  # pragma: no cover - needs GCP
        """Begin the streaming pull.

        Requires a running loop to hand messages back to, so it is started lazily by the
        caller that owns one.
        """
        if self._subscription_future is None:
            self._subscription_future = self.run_subscriber(asyncio.get_event_loop())

    async def stop(self) -> None:  # pragma: no cover - needs GCP
        if self._subscription_future is not None:
            self._subscription_future.cancel()
            self._subscription_future = None

    async def publish_duplicate(self, event: AriadneEvent) -> None:  # pragma: no cover
        """Publish the same event again, to demonstrate duplicate safety."""
        await self.publish(event)

    def note_duplicate_suppressed(self) -> None:
        self.stats.duplicates_suppressed += 1

    @property
    def dead_letters(self) -> list[DeadLetter]:
        """Always empty: Pub/Sub owns the dead-letter topic.

        Reporting an empty list here is not a claim that nothing was dead-lettered - it is a
        statement that this process is not the place to look. The dead-letter topic named in
        ``dead_letter_topic`` is.
        """
        return []

    @property
    def published_events(self) -> list[AriadneEvent]:
        return []

    def snapshot(self) -> dict[str, Any]:
        return {
            "published": self.stats.published,
            "delivered": self.stats.delivered,
            "duplicates_suppressed": self.stats.duplicates_suppressed,
            "retried": self.stats.retried,
            "dead_lettered": self.stats.dead_lettered,
            "failed": self.stats.failed,
            "queued": 0,
            "transport": "pubsub",
            "dead_letter_topic": self.dead_letter_topic,
        }

    async def publish(self, event: AriadneEvent) -> None:  # pragma: no cover - needs GCP
        from google.cloud import pubsub_v1  # type: ignore[import-not-found]

        publisher = pubsub_v1.PublisherClient()
        path = publisher.topic_path(self.project_id, self.topic)
        payload = event.model_dump_json().encode("utf-8")
        publisher.publish(
            path,
            payload,
            event_id=event.event_id,
            event_type=str(event.event_type),
            idempotency_key=event.idempotency_key,
        ).result(timeout=30)
        self.stats.published += 1

    async def drain(self) -> None:  # pragma: no cover - streaming pull runs continuously
        raise NotImplementedError(
            "PubSubEventBus is driven by a streaming pull subscriber, not by drain()"
        )

    def run_subscriber(self, loop: asyncio.AbstractEventLoop) -> Any:  # pragma: no cover
        """Start a streaming pull. Acks only after the handler succeeds.

        Acking before the work is done would convert an at-least-once system into an
        at-most-once one, and a crashed worker would silently lose the audit.
        """
        from google.cloud import pubsub_v1  # type: ignore[import-not-found]

        from backend.events.schemas import parse_event

        subscriber = pubsub_v1.SubscriberClient()
        path = subscriber.subscription_path(self.project_id, self.subscription)

        logger = logging.getLogger("ariadne.pubsub")

        def callback(message: Any) -> None:
            event_id = message.attributes.get("event_id", "<unknown>")
            try:
                event = parse_event(json.loads(message.data.decode("utf-8")))
            except Exception:
                # A message that cannot even be parsed will never parse. Nacking it would
                # loop until the delivery cap; acking lets the malformed message go while
                # the error stays in the log.
                logger.exception("unparseable message, dropping", extra={"event_id": event_id})
                message.ack()
                return

            try:
                for handler in self._handlers:
                    asyncio.run_coroutine_threadsafe(handler(event), loop).result(timeout=300)
                message.ack()
            except Exception:
                logger.exception("handler failed, nacking", extra={"event_id": event_id})
                message.nack()  # let Pub/Sub redeliver, then dead-letter

        return subscriber.subscribe(path, callback=callback)


def build_event_bus(settings: Any | None = None) -> LocalEventBus | PubSubEventBus:
    from backend.config import get_settings

    config = settings or get_settings()
    if config.event_bus == "pubsub":
        return PubSubEventBus(
            project_id=config.gcp_project_id,
            topic=config.pubsub_model_topic,
            subscription=f"{config.pubsub_model_topic}.worker",
            dead_letter_topic=config.pubsub_dead_letter_topic,
            max_attempts=config.max_delivery_attempts,
        )
    return LocalEventBus(
        max_attempts=config.max_delivery_attempts,
        base_delay=config.retry_base_delay_seconds,
        max_delay=config.retry_max_delay_seconds,
    )
