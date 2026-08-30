"""Actually talk to the other side of a connection.

The point of this module is that "Test connection" must mean something. A status field a
form sets to OK is worse than no status field, because it tells a governance team a link is
healthy on the evidence that somebody once typed a URL.

So every probe here performs real I/O and reports the individual checks it ran, not a
verdict. `ProbeResult.checks` is a list of named observations, each with what it saw, so a
green tick is auditable rather than believed - the same principle the verifier applies to
verdicts, applied to infrastructure.

**Failures here are infrastructure failures and stay that way.** A connection that cannot be
reached produces a FAILED connection, never a CONTRADICTED claim. Keeping those two apart is
the reason `TargetModelError` is retryable and separate from a verdict, and the reason this
module returns a `ProbeResult` rather than anything the verifier can see.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Any

from backend.core.clock import Clock, SystemClock
from backend.core.configuration import (
    Connection,
    ConnectionKind,
    ProbeCheck,
    ProbeResult,
    TransportKind,
)


class ConnectionProber:
    """Runs a real check appropriate to what the connection connects to."""

    def __init__(self, *, clock: Clock | None = None, timeout: float | None = None) -> None:
        self._clock = clock or SystemClock()
        self._timeout_override = timeout

    def probe(self, connection: Connection) -> ProbeResult:
        started = time.perf_counter()
        checks: list[ProbeCheck] = []
        error: str | None = None
        try:
            checks = self._dispatch(connection)
        except Exception as exc:  # noqa: BLE001 - any failure is a failed probe, reported
            error = f"{type(exc).__name__}: {exc}"
            checks.append(
                ProbeCheck(name="reachable", passed=False, detail=error)
            )
        return ProbeResult(
            connection_id=connection.id,
            ok=bool(checks) and all(check.passed for check in checks),
            checks=checks,
            error=error,
            latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
            checked_at=self._clock.now(),
        )

    # -- dispatch ----------------------------------------------------------------------

    def _dispatch(self, connection: Connection) -> list[ProbeCheck]:
        if connection.transport is TransportKind.IN_PROCESS:
            return self._probe_in_process(connection)
        if connection.transport is TransportKind.VERTEX_AI:
            return self._probe_vertex(connection)
        if connection.transport is TransportKind.PUBSUB:
            return self._probe_pubsub(connection)
        if connection.transport is TransportKind.HTTP:
            return self._probe_http(connection)
        return [
            ProbeCheck(
                name="transport",
                passed=False,
                detail=f"no probe is implemented for {connection.transport}",
            )
        ]

    # -- the synthetic laboratory ------------------------------------------------------

    def _probe_in_process(self, connection: Connection) -> list[ProbeCheck]:
        """The laboratory model. Real, but local - and labelled so nobody mistakes it."""
        from backend.experiment_engine.target_model import KNOWN_VERSIONS, get_target_model

        checks = [
            ProbeCheck(
                name="transport",
                passed=True,
                detail="in-process synthetic laboratory — not an external model",
            )
        ]
        version = connection.model_version or KNOWN_VERSIONS[0]
        if version not in KNOWN_VERSIONS:
            checks.append(
                ProbeCheck(
                    name="model version",
                    passed=False,
                    detail=f"{version!r} is not one of {list(KNOWN_VERSIONS)}",
                )
            )
            return checks
        model = get_target_model(version)
        output = model.predict({"urgency_marker": 0.8, "signal_b": 0.3, "signal_c": 0.6})
        checks.append(
            ProbeCheck(name="model version", passed=True, detail=f"v{version} resolved")
        )
        checks.append(
            ProbeCheck(
                name="response schema",
                passed=isinstance(output.score, float),
                detail=f"continuous score returned: {output.score:.4f}",
            )
        )
        return checks

    # -- Vertex AI ---------------------------------------------------------------------

    def _probe_vertex(self, connection: Connection) -> list[ProbeCheck]:
        """One real generate_content call. Nothing else proves the path works."""
        checks: list[ProbeCheck] = []
        if not connection.project:
            return [ProbeCheck(name="project", passed=False, detail="no GCP project set")]

        try:
            from google import genai  # type: ignore[import-not-found,attr-defined]
        except ImportError as exc:
            return [
                ProbeCheck(name="sdk", passed=False, detail=f"google-genai missing: {exc}")
            ]
        checks.append(ProbeCheck(name="sdk", passed=True, detail="google-genai importable"))

        client = genai.Client(
            vertexai=True,
            project=connection.project,
            location=connection.region or "global",
        )
        model = connection.model_id or "gemini-3.5-flash"
        response = client.models.generate_content(
            model=model,
            contents="Reply with the single word: ok",
            config={"max_output_tokens": 16, "thinking_config": {"thinking_budget": 0}},
        )
        text = (getattr(response, "text", "") or "").strip()
        checks.append(
            ProbeCheck(
                name="authentication",
                passed=True,
                detail=f"credentials accepted for project {connection.project}",
            )
        )
        checks.append(
            ProbeCheck(
                name="model reachable",
                passed=bool(text),
                detail=f"{model} responded with {text[:40]!r}" if text
                else f"{model} returned an empty response",
            )
        )
        return checks

    # -- Pub/Sub -----------------------------------------------------------------------

    def _probe_pubsub(self, connection: Connection) -> list[ProbeCheck]:
        """Resolve the topic. Publishing a probe message would pollute a real subscription."""
        checks: list[ProbeCheck] = []
        if not connection.project:
            return [ProbeCheck(name="project", passed=False, detail="no GCP project set")]
        try:
            from google.cloud import pubsub_v1  # type: ignore[import-not-found,attr-defined]
        except ImportError as exc:
            return [
                ProbeCheck(name="sdk", passed=False, detail=f"google-cloud-pubsub missing: {exc}")
            ]
        checks.append(ProbeCheck(name="sdk", passed=True, detail="google-cloud-pubsub importable"))

        publisher = pubsub_v1.PublisherClient()
        topic_path = publisher.topic_path(connection.project, connection.endpoint)
        publisher.get_topic(request={"topic": topic_path})
        checks.append(
            ProbeCheck(name="topic", passed=True, detail=f"{topic_path} exists and is readable")
        )
        return checks

    # -- plain HTTP --------------------------------------------------------------------

    def _probe_http(self, connection: Connection) -> list[ProbeCheck]:
        """Reach the endpoint and report what came back, including an auth rejection.

        A 401 or 403 is reported as an *authentication* failure rather than a generic one,
        because those send an operator somewhere completely different from a 500.
        """
        checks: list[ProbeCheck] = []
        timeout = self._timeout_override or connection.timeout_seconds
        request = urllib.request.Request(connection.endpoint, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                body = response.read(2048)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return [
                    ProbeCheck(
                        name="authentication",
                        passed=False,
                        detail=f"endpoint rejected the request with HTTP {exc.code}",
                    )
                ]
            return [
                ProbeCheck(
                    name="reachable",
                    passed=False,
                    detail=f"endpoint returned HTTP {exc.code}",
                )
            ]
        except urllib.error.URLError as exc:
            return [
                ProbeCheck(name="reachable", passed=False, detail=f"unreachable: {exc.reason}")
            ]

        checks.append(
            ProbeCheck(name="reachable", passed=True, detail=f"HTTP {status} from endpoint")
        )
        checks.append(
            ProbeCheck(
                name="authentication",
                passed=True,
                detail="endpoint did not reject the request",
            )
        )
        if connection.kind is ConnectionKind.MODEL_ENDPOINT:
            checks.append(
                ProbeCheck(
                    name="response body",
                    passed=bool(body),
                    detail=f"{len(body)} bytes received"
                    if body
                    else "endpoint returned an empty body",
                )
            )
        return checks


def apply_probe(connection: Connection, result: ProbeResult) -> Connection:
    """Fold a probe outcome into the connection record.

    The only path by which a connection becomes OK. Status is earned here or not at all,
    which is what stops the create/update API from asserting health it never observed.
    """
    from backend.core.configuration import ConnectionStatus

    detail: dict[str, Any] = {check.name: check.detail for check in result.checks}
    return connection.model_copy(
        update={
            "status": ConnectionStatus.OK if result.ok else ConnectionStatus.FAILED,
            "last_checked_at": result.checked_at,
            "last_success_at": result.checked_at if result.ok else connection.last_success_at,
            "last_failure_at": connection.last_failure_at if result.ok else result.checked_at,
            "last_error": None if result.ok else (result.error or "one or more checks failed"),
            "probe_detail": detail,
            "updated_at": result.checked_at,
        }
    )
