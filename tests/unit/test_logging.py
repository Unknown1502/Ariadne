"""Structured logging: the fields, and the context that carries them.

Logs are how an investigation's journey through four agents is reassembled after the fact,
which makes them evidence — and this module was the least-covered one in the repository. The
`trace()` context manager, the thing that binds a trace id across everything logged inside
it, had never executed under test at all.

Two properties matter more than the rest and are checked hardest here:

  - **Context does not leak between traces.** The module deliberately defaults its ContextVar
    to None rather than `{}`, because a mutable default is shared by every context that never
    set one, and a single in-place write would smear one investigation's fields across
    unrelated logs. The comment saying so is now a test.
  - **Every line is machine-parseable.** Cloud Logging turns JSON on stdout into structured
    fields with no shipping agent. One unserializable value would silently degrade that to
    plain text, so the formatter is asserted to always produce valid JSON.
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest

from backend.observability.logging import (
    OBSERVABILITY_FIELDS,
    JsonFormatter,
    configure_logging,
    current_trace_id,
    get_logger,
    log_event,
    new_trace_id,
    trace,
)


def record(**kwargs) -> logging.LogRecord:
    extra = kwargs.pop("ariadne", None)
    made = logging.LogRecord(
        name=kwargs.pop("name", "ariadne.test"),
        level=kwargs.pop("level", logging.INFO),
        pathname=__file__,
        lineno=1,
        msg=kwargs.pop("msg", "hello"),
        args=kwargs.pop("args", ()),
        exc_info=kwargs.pop("exc_info", None),
    )
    if extra is not None:
        made.ariadne = extra
    return made


@pytest.fixture
def lines():
    """Lines formatted at emit time, the way a real handler does it.

    This matters: the formatter reads the ambient trace context *when it formats*. Holding
    records and rendering them afterwards would read a context that has already been
    released, and would test the opposite of what production does.
    """
    captured: list[dict] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(json.loads(self.format(record)))

    handler = Capture()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG)
    try:
        yield captured
    finally:
        root.handlers[:] = saved
        root.setLevel(level)


class TestTheJsonLine:
    def test_it_is_a_single_parseable_json_object(self) -> None:
        payload = json.loads(JsonFormatter().format(record()))
        assert payload["severity"] == "INFO"
        assert payload["message"] == "hello"
        assert payload["logger"] == "ariadne.test"
        assert "timestamp" in payload

    def test_message_arguments_are_interpolated(self) -> None:
        payload = json.loads(
            JsonFormatter().format(record(msg="ran %d cases", args=(12,)))
        )
        assert payload["message"] == "ran 12 cases"

    def test_ariadne_fields_are_merged_into_the_line(self) -> None:
        payload = json.loads(
            JsonFormatter().format(record(ariadne={"investigation_id": "INV-1", "retries": 2}))
        )
        assert payload["investigation_id"] == "INV-1"
        assert payload["retries"] == 2

    def test_a_non_dict_extra_is_ignored_rather_than_crashing(self) -> None:
        """A logger is the worst place to raise. A malformed extra must not take the line."""
        payload = json.loads(JsonFormatter().format(record(ariadne="not-a-dict")))
        assert payload["message"] == "hello"

    def test_unserializable_values_degrade_instead_of_raising(self) -> None:
        payload = json.loads(JsonFormatter().format(record(ariadne={"clock": object()})))
        assert isinstance(payload["clock"], str)

    def test_exceptions_are_captured_as_text(self) -> None:
        try:
            raise ValueError("probe failed")
        except ValueError:
            import sys

            payload = json.loads(JsonFormatter().format(record(exc_info=sys.exc_info())))
        assert "ValueError: probe failed" in payload["exception"]

    def test_keys_are_sorted_so_lines_diff_cleanly(self) -> None:
        rendered = JsonFormatter().format(record(ariadne={"z": 1, "a": 2}))
        assert list(json.loads(rendered)) == sorted(json.loads(rendered))


class TestTraceBinding:
    def test_a_trace_id_is_attached_to_everything_inside(self, lines) -> None:
        with trace("trace-abc"):
            get_logger("ariadne.test").info("inside")
        assert lines and all(line["trace_id"] == "trace-abc" for line in lines)

    def test_it_generates_an_id_when_none_is_given(self) -> None:
        with trace() as generated:
            assert generated == current_trace_id()
            assert uuid.UUID(hex=generated)

    def test_the_id_is_released_on_exit(self) -> None:
        with trace("trace-abc"):
            assert current_trace_id() == "trace-abc"
        assert current_trace_id() is None

    def test_fields_bound_to_the_trace_appear_on_every_line(self, lines) -> None:
        with trace("t", investigation_id="INV-9"):
            get_logger("ariadne.test").info("first")
            get_logger("ariadne.other").info("second")
        messages = [line for line in lines if line["message"] in {"first", "second"}]
        assert [line["investigation_id"] for line in messages] == ["INV-9", "INV-9"]

    def test_none_valued_fields_are_dropped(self) -> None:
        """An absent field should be absent, not present-and-null."""
        with trace("t", investigation_id=None, agent_id="verifier"):
            payload = json.loads(JsonFormatter().format(record()))
        assert "investigation_id" not in payload
        assert payload["agent_id"] == "verifier"

    def test_nested_traces_inherit_and_extend_the_outer_fields(self) -> None:
        # Deliberately nested rather than combined: the nesting is what is under test.
        with trace("outer", model_version="1.0.0"):  # noqa: SIM117
            with trace("inner", agent_id="verifier"):
                payload = json.loads(JsonFormatter().format(record()))
                assert payload["model_version"] == "1.0.0"
                assert payload["agent_id"] == "verifier"
                assert payload["trace_id"] == "inner"

    def test_the_outer_context_is_restored_after_a_nested_trace(self) -> None:
        with trace("outer", model_version="1.0.0"):
            with trace("inner", agent_id="verifier"):
                pass
            payload = json.loads(JsonFormatter().format(record()))
            assert payload["model_version"] == "1.0.0"
            assert "agent_id" not in payload
            assert payload["trace_id"] == "outer"

    def test_context_does_not_leak_after_the_block(self) -> None:
        """The reason the ContextVar defaults to None instead of an empty dict."""
        with trace("t", investigation_id="INV-9"):
            pass
        payload = json.loads(JsonFormatter().format(record()))
        assert "investigation_id" not in payload
        assert "trace_id" not in payload

    def test_context_is_released_even_when_the_block_raises(self) -> None:
        with pytest.raises(RuntimeError), trace("t", investigation_id="INV-9"):
            raise RuntimeError("boom")
        assert current_trace_id() is None
        assert "investigation_id" not in json.loads(JsonFormatter().format(record()))

    def test_the_span_records_its_own_duration(self, lines) -> None:
        with trace("t"):
            pass
        spans = [line for line in lines if line["message"] == "span complete"]
        assert spans and spans[-1]["latency_ms"] >= 0.0

    def test_trace_ids_are_unique(self) -> None:
        assert len({new_trace_id() for _ in range(200)}) == 200


class TestLogEvent:
    def test_it_carries_the_observability_fields(self, lines) -> None:
        fields = {name: f"value-{name}" for name in OBSERVABILITY_FIELDS}
        log_event(get_logger("ariadne.test"), "investigation complete", **fields)
        line = lines[-1]
        for name in OBSERVABILITY_FIELDS:
            assert line[name] == f"value-{name}"

    def test_none_valued_fields_are_omitted(self, lines) -> None:
        log_event(get_logger("ariadne.test"), "partial", retries=None, latency_ms=12.5)
        line = lines[-1]
        assert "retries" not in line
        assert line["latency_ms"] == 12.5

    def test_an_explicit_field_wins_over_the_ambient_one(self, lines) -> None:
        with trace("t", agent_id="investigator"):
            log_event(get_logger("ariadne.test"), "handoff", agent_id="verifier")
        handoff = [line for line in lines if line["message"] == "handoff"]
        assert handoff[-1]["agent_id"] == "verifier"


class TestConfiguration:
    @pytest.fixture(autouse=True)
    def restore_root(self):
        root = logging.getLogger()
        saved, level = list(root.handlers), root.level
        yield
        root.handlers[:] = saved
        root.setLevel(level)

    def test_json_is_the_default_format(self) -> None:
        configure_logging()
        assert isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)

    def test_a_plain_text_format_can_be_selected(self) -> None:
        configure_logging(fmt="text")
        formatter = logging.getLogger().handlers[0].formatter
        assert not isinstance(formatter, JsonFormatter)

    def test_calling_it_twice_does_not_duplicate_handlers(self) -> None:
        configure_logging()
        configure_logging()
        assert len(logging.getLogger().handlers) == 1

    def test_the_level_is_applied(self) -> None:
        configure_logging(level="warning")
        assert logging.getLogger().level == logging.WARNING

    def test_an_unknown_level_falls_back_to_info(self) -> None:
        """A typo in configuration should not silence the system."""
        configure_logging(level="chatty")
        assert logging.getLogger().level == logging.INFO
