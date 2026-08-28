"""Structured logging.

Every log line is one JSON object carrying the fields prompt 15 requires: trace_id,
event_id, idempotency_key, investigation_id, agent_id, model_version, distribution_version,
state, input/output hashes, latency, retries, and a cost estimate.

The format is chosen for Cloud Logging, which parses JSON on stdout into structured fields
automatically - so the same lines that are readable locally become queryable in the cloud
without a shipping agent.

A note on the cost field: it is an *estimate* computed from token counts and published list
prices, and it is labelled as such everywhere it appears. It is not billing data, and the
project never presents it as though it were.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_trace_id: ContextVar[str | None] = ContextVar("ariadne_trace_id", default=None)
_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "ariadne_log_context", default=None
)
"""None rather than {} as the default.

A mutable default on a ContextVar is shared by every context that never set one, so a single
`.get()[...] = value` anywhere would leak fields into unrelated traces. Readers normalize
None to an empty dict instead."""

OBSERVABILITY_FIELDS = (
    "trace_id",
    "event_id",
    "idempotency_key",
    "investigation_id",
    "agent_id",
    "model_version",
    "distribution_version",
    "state",
    "tool_calls",
    "input_hash",
    "output_hash",
    "latency_ms",
    "retries",
    "estimated_cost_usd",
)


class JsonFormatter(logging.Formatter):
    """Renders a record as a single JSON line, merging the ambient context."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        payload.update(_context.get() or {})
        trace = _trace_id.get()
        if trace:
            payload["trace_id"] = trace
        extra = getattr(record, "ariadne", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, sort_keys=True)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install the root handler. Safe to call more than once."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter()
        if fmt == "json"
        else logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def new_trace_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def trace(trace_id: str | None = None, **fields: Any):
    """Bind a trace id and context fields for the duration of a block.

    Everything logged inside carries these fields, so one investigation's journey through
    four agents and a dozen steps can be reassembled from logs with a single query.
    """
    token_trace = _trace_id.set(trace_id or new_trace_id())
    merged = {**(_context.get() or {}), **{k: v for k, v in fields.items() if v is not None}}
    token_context = _context.set(merged)
    started = time.perf_counter()
    try:
        yield _trace_id.get()
    finally:
        elapsed = (time.perf_counter() - started) * 1000.0
        logging.getLogger("ariadne.trace").debug(
            "span complete", extra={"ariadne": {"latency_ms": round(elapsed, 3)}}
        )
        _context.reset(token_context)
        _trace_id.reset(token_trace)


def log_event(logger: logging.Logger, message: str, **fields: Any) -> None:
    """Emit one structured line with the observability fields."""
    logger.info(message, extra={"ariadne": {k: v for k, v in fields.items() if v is not None}})


def current_trace_id() -> str | None:
    return _trace_id.get()
