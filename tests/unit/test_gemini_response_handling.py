"""Gemini responses that are unusable for a reason retrying cannot fix.

Found by checking the client against the live google-genai docs rather than against memory:
`finish_reason` lives on the *candidate*, and the client never read it.

The consequence was subtle and expensive. A response truncated at `max_output_tokens` comes
back as JSON cut off mid-structure. The parser calls that "malformed", which is classified
retryable, so the agent tries again - at temperature 0, producing the identical truncation,
until the loop budget drains. The operator then reads "malformed JSON" and goes looking for
a prompt bug that does not exist.

These tests use doubles shaped like the real SDK response. They cover the client's handling
logic, not Gemini itself; that boundary is recorded in docs/limitations.md.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.agents.llm import GeminiClient, LLMRequest
from backend.core.errors import AgentResponseRejected


class FakeFinishReason:
    """google-genai returns an enum whose `.name` carries the reason."""

    def __init__(self, name: str) -> None:
        self.name = name


class FakeCandidate:
    def __init__(self, finish_reason: Any) -> None:
        self.finish_reason = finish_reason


class FakeUsage:
    def __init__(self, prompt: int = 120, completion: int = 40) -> None:
        self.prompt_token_count = prompt
        self.candidates_token_count = completion


class FakeResponse:
    def __init__(
        self,
        *,
        text: str = '{"ok": true}',
        finish_reason: Any = None,
        candidates: list[Any] | None = None,
        prompt_feedback: Any = None,
    ) -> None:
        self.text = text
        self.usage_metadata = FakeUsage()
        self.prompt_feedback = prompt_feedback
        if candidates is not None:
            self.candidates = candidates
        else:
            self.candidates = [FakeCandidate(finish_reason or FakeFinishReason("STOP"))]


class FakeModels:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls = 0

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls += 1
        self.last_kwargs = kwargs
        return self._response


class FakeGenAIClient:
    def __init__(self, response: Any) -> None:
        self.models = FakeModels(response)


def client_returning(response: Any) -> tuple[GeminiClient, FakeGenAIClient]:
    client = GeminiClient(model="gemini-3.5-flash", api_key="test-key")
    fake = FakeGenAIClient(response)
    client._client = fake  # skip the network entirely
    return client, fake


REQUEST = LLMRequest(system="be precise", user="compile this claim", task="compile_claim")


class TestTruncation:
    def test_truncated_output_is_rejected_with_its_real_cause(self) -> None:
        client, _ = client_returning(
            FakeResponse(
                text='{"subject": "urgency_mark',  # cut off mid-string
                finish_reason=FakeFinishReason("MAX_TOKENS"),
            )
        )
        with pytest.raises(AgentResponseRejected, match="max_output_tokens"):
            client.generate(REQUEST)

    def test_the_error_points_at_the_actual_fix(self) -> None:
        client, _ = client_returning(
            FakeResponse(text="{", finish_reason=FakeFinishReason("MAX_TOKENS"))
        )
        with pytest.raises(AgentResponseRejected, match="GEMINI_MAX_OUTPUT_TOKENS"):
            client.generate(REQUEST)

    def test_truncation_is_not_retryable(self) -> None:
        # The whole point: temperature is 0, so a retry reproduces the same truncation.
        assert AgentResponseRejected.retryable is False


class TestSafetyAndBlocking:
    @pytest.mark.parametrize("reason", ["SAFETY", "RECITATION", "BLOCKLIST"])
    def test_a_stopped_generation_is_rejected(self, reason: str) -> None:
        client, _ = client_returning(
            FakeResponse(text="", finish_reason=FakeFinishReason(reason))
        )
        with pytest.raises(AgentResponseRejected, match=reason):
            client.generate(REQUEST)

    def test_a_blocked_prompt_is_reported_as_a_block(self) -> None:
        # The explanation text is attacker-controlled, so a blocked prompt is a plausible
        # input rather than a bug - and it should say so.
        blocked = FakeResponse(text="", prompt_feedback=type("F", (), {"block_reason": "SAFETY"})())
        client, _ = client_returning(blocked)
        with pytest.raises(AgentResponseRejected, match="blocked the prompt"):
            client.generate(REQUEST)

    def test_no_candidates_is_rejected_rather_than_parsed_as_empty(self) -> None:
        client, _ = client_returning(FakeResponse(text="", candidates=[]))
        with pytest.raises(AgentResponseRejected, match="no candidates"):
            client.generate(REQUEST)


class TestHealthyResponses:
    def test_a_normal_response_passes_through(self) -> None:
        client, _ = client_returning(FakeResponse(text='{"ok": true}'))
        result = client.generate(REQUEST)
        assert result.text == '{"ok": true}'
        assert result.finish_reason == "STOP"
        assert result.prompt_tokens == 120
        assert result.completion_tokens == 40

    def test_the_real_finish_reason_reaches_the_audit_trail(self) -> None:
        # Previously hardcoded to "stop", so the audit trail could not distinguish a clean
        # completion from anything else.
        client, _ = client_returning(
            FakeResponse(finish_reason=FakeFinishReason("STOP"))
        )
        assert client.generate(REQUEST).finish_reason == "STOP"

    def test_a_plain_string_finish_reason_is_handled(self) -> None:
        # Not every SDK version returns an enum.
        client, _ = client_returning(FakeResponse(finish_reason="STOP"))
        assert client.generate(REQUEST).finish_reason == "STOP"

    def test_cost_is_estimated_from_real_token_counts(self) -> None:
        client, _ = client_returning(FakeResponse())
        assert client.generate(REQUEST).estimated_cost_usd() > 0


class TestRequestConstruction:
    def test_the_configured_token_ceiling_is_applied(self) -> None:
        client = GeminiClient(model="gemini-3.5-flash", api_key="k", max_output_tokens=512)
        fake = FakeGenAIClient(FakeResponse())
        client._client = fake
        client.generate(LLMRequest(system="s", user="u", task="t", max_output_tokens=8192))
        assert fake.models.last_kwargs["config"]["max_output_tokens"] == 512

    def test_thinking_is_disabled_so_the_budget_reaches_the_answer(self) -> None:
        """Gemini 2.5 spends output tokens on internal reasoning before it answers.

        Found by running the Investigator evaluation against live Vertex AI for the first
        time: *every* call failed with MAX_TOKENS at a 2048-token ceiling, because thinking
        consumed the budget before any JSON was produced. The agent path had never been
        exercised live, so nothing caught it - `docs/limitations.md` says the agent-side
        Gemini client is unverified, and this is what that gap was hiding.

        The target-model adapter already sets `thinking_budget: 0` for exactly this reason
        (`gemini_target.py`). The two clients had drifted: one had the fix, the other had
        the same bug the fix was written for. A claim compiler emits a small structured
        object and needs no reasoning tokens to do it.
        """
        client, fake = client_returning(FakeResponse())
        client.generate(REQUEST)
        assert fake.models.last_kwargs["config"]["thinking_config"] == {"thinking_budget": 0}

    def test_a_thinking_budget_can_be_raised_when_a_task_needs_it(self) -> None:
        """Disabled by default, not disabled by force - the ceiling stays configurable."""
        client = GeminiClient(model="gemini-3.5-flash", api_key="k", thinking_budget=512)
        fake = FakeGenAIClient(FakeResponse())
        client._client = fake
        client.generate(REQUEST)
        assert fake.models.last_kwargs["config"]["thinking_config"] == {"thinking_budget": 512}

    def test_json_mode_is_requested(self) -> None:
        client, fake = client_returning(FakeResponse())
        client.generate(REQUEST)
        assert fake.models.last_kwargs["config"]["response_mime_type"] == "application/json"

    def test_dict_config_is_used(self) -> None:
        # Verified against the google-genai docs: "All API methods support Pydantic types as
        # well as standard dictionaries for passing parameters."
        client, fake = client_returning(FakeResponse())
        client.generate(REQUEST)
        assert isinstance(fake.models.last_kwargs["config"], dict)


class TestRetryLoopIntegration:
    def test_a_rejected_response_burns_one_attempt_not_the_budget(self) -> None:
        from backend.agents.audit import AuditSink
        from backend.agents.base import AgentBase
        from backend.agents.registry import INVESTIGATOR_MANIFEST
        from backend.core.clock import ManualClock
        from tests.factories import T0

        recorded: list[Any] = []

        class CapturingSink(AuditSink):
            def record_invocation(self, invocation: Any) -> None:
                recorded.append(invocation)

            def record_tool_call(self, call: Any) -> None:
                pass

            def record_message(self, message: Any) -> None:
                pass

        class TruncatingLLM:
            model_name = "gemini-3.5-flash"
            is_language_model = True

            def __init__(self) -> None:
                self.calls = 0

            def generate(self, request: LLMRequest) -> Any:
                self.calls += 1
                raise AgentResponseRejected("Gemini hit max_output_tokens")

        llm = TruncatingLLM()
        agent = AgentBase(
            INVESTIGATOR_MANIFEST, llm, clock=ManualClock(T0), audit=CapturingSink()
        )

        with pytest.raises(AgentResponseRejected):
            agent.reason(REQUEST, investigation_id="INV-1", validate=lambda payload: payload)

        assert llm.calls == 1, "a non-retryable rejection must not be retried"
        assert INVESTIGATOR_MANIFEST.loop_budget > 1, "the budget was available to burn"

    def test_the_failure_still_leaves_an_audit_row(self) -> None:
        # An agent failure that leaves no trace is indistinguishable from one that never
        # ran. That defect emptied this system's audit trail once already.
        from backend.agents.audit import AuditSink
        from backend.agents.base import AgentBase
        from backend.agents.registry import INVESTIGATOR_MANIFEST
        from backend.core.clock import ManualClock
        from tests.factories import T0

        recorded: list[Any] = []

        class CapturingSink(AuditSink):
            def record_invocation(self, invocation: Any) -> None:
                recorded.append(invocation)

            def record_tool_call(self, call: Any) -> None:
                pass

            def record_message(self, message: Any) -> None:
                pass

        class TruncatingLLM:
            model_name = "gemini-3.5-flash"
            is_language_model = True

            def generate(self, request: LLMRequest) -> Any:
                raise AgentResponseRejected("Gemini hit max_output_tokens")

        agent = AgentBase(
            INVESTIGATOR_MANIFEST, TruncatingLLM(), clock=ManualClock(T0), audit=CapturingSink()
        )
        with pytest.raises(AgentResponseRejected):
            agent.reason(REQUEST, investigation_id="INV-1", validate=lambda payload: payload)

        assert len(recorded) == 1
        assert recorded[0].succeeded is False
        assert recorded[0].error_code == "AgentResponseRejected"
