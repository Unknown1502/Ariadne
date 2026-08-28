"""PubSubEventBus against a real Pub/Sub emulator.

Every network-touching method on `PubSubEventBus` — `publish`, `run_subscriber`, `stop`,
`publish_duplicate` — carried a `# pragma: no cover - needs GCP` comment. That is an honest
label, and it also means zero lines of this class's real behaviour had ever executed: the
streaming-pull callback, the ack/nack decision, the JSON round-trip through real message
bytes and attributes, all of it existed only as code that *looked* right.

These tests run it against ``gcr.io/google.com/cloudsdktool/cloud-sdk:emulators`` — the real
`google-cloud-pubsub` wire protocol and the real streaming-pull subscriber. What they prove:
the adapter's publish/ack/nack logic is correct against the actual client library. What they
do not prove: IAM, cross-region delivery, or production-scale ordering guarantees — those
still need a deployed project, and `docs/limitations.md` says so plainly.

Skipped entirely unless ``PUBSUB_EMULATOR_HOST`` is set. To run these:

    docker run -d -p 8085:8085 gcr.io/google.com/cloudsdktool/cloud-sdk:emulators \\
        gcloud beta emulators pubsub start --host-port=0.0.0.0:8085
    PUBSUB_EMULATOR_HOST=localhost:8085 pytest tests/integration/test_pubsub_emulator.py
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    "PUBSUB_EMULATOR_HOST" not in os.environ,
    reason="requires a live Pub/Sub emulator; set PUBSUB_EMULATOR_HOST to run",
)

pubsub_v1 = pytest.importorskip("google.cloud.pubsub_v1")

from backend.core.enums import EventType  # noqa: E402
from backend.events.bus import PubSubEventBus  # noqa: E402
from backend.events.schemas import (  # noqa: E402
    ModelVersionDeployedPayload,
    make_event,
    parse_event,
)
from tests.factories import T0  # noqa: E402

PROJECT = f"ariadne-emu-{uuid.uuid4().hex[:8]}"
CALLBACK_TIMEOUT = 15.0


@pytest.fixture
def topic_and_subscription() -> tuple[str, str]:
    """A fresh topic + subscription per test, provisioned the way Terraform would.

    `PubSubEventBus.publish()` assumes its topic already exists — production infra
    provisions topics, application code does not — so the test fixture plays that role.
    """
    topic_name = f"events-{uuid.uuid4().hex[:12]}"
    sub_name = f"{topic_name}.worker"

    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()
    topic_path = publisher.topic_path(PROJECT, topic_name)
    sub_path = subscriber.subscription_path(PROJECT, sub_name)

    publisher.create_topic(request={"name": topic_path})
    subscriber.create_subscription(request={"name": sub_path, "topic": topic_path})

    yield topic_name, sub_name
    subscriber.close()


@pytest.fixture
def bus(topic_and_subscription: tuple[str, str]) -> PubSubEventBus:
    topic, subscription = topic_and_subscription
    return PubSubEventBus(
        project_id=PROJECT, topic=topic, subscription=subscription,
        dead_letter_topic="events-dlq", max_attempts=5,
    )


def sample_event(version: str = "2.0.0"):
    return make_event(
        EventType.MODEL_VERSION_DEPLOYED,
        ModelVersionDeployedPayload(
            model_id="synthetic-triage", model_version=version,
            distribution_version="baseline_2024.1", deployed_at=T0,
        ),
        aggregate_id="synthetic-triage", aggregate_version=version, occurred_at=T0,
    )


class TestPublishAgainstRealPubSub:
    @pytest.mark.asyncio
    async def test_publish_delivers_real_bytes_to_the_topic(
        self, bus: PubSubEventBus, topic_and_subscription: tuple[str, str]
    ) -> None:
        _, subscription = topic_and_subscription
        event = sample_event()
        await bus.publish(event)

        subscriber = pubsub_v1.SubscriberClient()
        sub_path = subscriber.subscription_path(PROJECT, subscription)
        response = subscriber.pull(
            request={"subscription": sub_path, "max_messages": 1}, timeout=10
        )
        assert len(response.received_messages) == 1

        message = response.received_messages[0].message
        restored = parse_event(json.loads(message.data.decode("utf-8")))
        assert restored.event_id == event.event_id
        assert restored.idempotency_key == event.idempotency_key
        assert message.attributes["idempotency_key"] == event.idempotency_key
        subscriber.close()

    @pytest.mark.asyncio
    async def test_publish_increments_the_stats(
        self, bus: PubSubEventBus, topic_and_subscription: tuple[str, str]
    ) -> None:
        assert bus.stats.published == 0
        await bus.publish(sample_event())
        assert bus.stats.published == 1

    @pytest.mark.asyncio
    async def test_publish_duplicate_sends_a_second_message(
        self, bus: PubSubEventBus, topic_and_subscription: tuple[str, str]
    ) -> None:
        """Pub/Sub itself does not deduplicate. Ariadne's idempotency layer must.

        This is not a bug to fix in the bus - it demonstrates why the claim() step exists at
        all: the transport's job is at-least-once delivery, not exactly-once semantics.
        """
        _, subscription = topic_and_subscription
        event = sample_event()
        await bus.publish(event)
        await bus.publish_duplicate(event)

        subscriber = pubsub_v1.SubscriberClient()
        sub_path = subscriber.subscription_path(PROJECT, subscription)
        response = subscriber.pull(
            request={"subscription": sub_path, "max_messages": 5}, timeout=10
        )
        assert len(response.received_messages) == 2
        ids = {
            json.loads(m.message.data.decode("utf-8"))["event_id"]
            for m in response.received_messages
        }
        assert ids == {event.event_id}  # same event_id, delivered twice - by design
        subscriber.close()


class TestStreamingPullAgainstRealPubSub:
    """The path that was 100% uncovered: `run_subscriber`'s callback, ack, and nack logic."""

    @pytest.mark.asyncio
    async def test_a_published_event_reaches_the_handler_parsed(
        self, bus: PubSubEventBus, topic_and_subscription: tuple[str, str]
    ) -> None:
        received: list = []
        done = asyncio.Event()

        async def handler(event) -> None:
            received.append(event)
            done.set()

        bus.subscribe(handler)
        loop = asyncio.get_running_loop()
        future = bus.run_subscriber(loop)
        try:
            sent = sample_event()
            await bus.publish(sent)
            await asyncio.wait_for(done.wait(), timeout=CALLBACK_TIMEOUT)

            assert len(received) == 1
            assert received[0].event_id == sent.event_id
            assert received[0].payload.model_version == "2.0.0"
        finally:
            future.cancel()

    @pytest.mark.asyncio
    async def test_a_malformed_message_is_dropped_not_looped(
        self, bus: PubSubEventBus, topic_and_subscription: tuple[str, str]
    ) -> None:
        """An unparseable message is acked and logged, never redelivered.

        Nacking it would retry forever against a message that will never parse - a permanent
        error masquerading as a transient one.
        """
        topic, subscription = topic_and_subscription
        received: list = []

        async def handler(event) -> None:
            received.append(event)

        bus.subscribe(handler)
        loop = asyncio.get_running_loop()
        future = bus.run_subscriber(loop)
        try:
            publisher = pubsub_v1.PublisherClient()
            topic_path = publisher.topic_path(PROJECT, topic)
            publisher.publish(topic_path, b"not valid json at all").result(timeout=10)

            # Give the emulator time to deliver and the callback time to drop it, then prove
            # it was not sitting around for redelivery.
            await asyncio.sleep(3.0)

            subscriber = pubsub_v1.SubscriberClient()
            sub_path = subscriber.subscription_path(PROJECT, subscription)
            leftover = subscriber.pull(
                request={"subscription": sub_path, "max_messages": 1}, timeout=5
            )
            subscriber.close()
            assert not leftover.received_messages, (
                "a malformed message must be acked, not redelivered"
            )
            assert received == []
        finally:
            future.cancel()

    @pytest.mark.asyncio
    async def test_a_failing_handler_causes_redelivery(
        self, bus: PubSubEventBus, topic_and_subscription: tuple[str, str]
    ) -> None:
        """nack() must trigger real redelivery, not just a return value nobody checks."""
        attempts = 0
        succeeded = asyncio.Event()

        async def flaky_handler(event) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                raise RuntimeError("simulated transient failure")
            succeeded.set()

        bus.subscribe(flaky_handler)
        loop = asyncio.get_running_loop()
        future = bus.run_subscriber(loop)
        try:
            await bus.publish(sample_event())
            await asyncio.wait_for(succeeded.wait(), timeout=CALLBACK_TIMEOUT)
            assert attempts >= 2, "a nacked message must be redelivered to the same handler"
        finally:
            future.cancel()
