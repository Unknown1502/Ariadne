"""The Gemini target-model adapter's parsing and transport logic.

This module ran against live Vertex AI before it had a single test — a coverage sweep found
it at 0%. That is the wrong order for the one module in the repository that spends money per
call: every branch below is a way a real API response can arrive, and discovering them
against the live endpoint costs both latency and billing.

No test here makes a network call. The transport is driven by a fake client shaped like the
google-genai response object, including the `finish_reason` enum whose `.name` attribute the
real SDK returns.
"""

from __future__ import annotations

import pytest

from backend.core.errors import ValidationError
from backend.experiment_engine.gemini_target import (
    GeminiTriageCodec,
    VertexGeminiTransport,
    build_gemini_target,
)

CASE = {"urgency_marker": 0.83, "signal_b": 0.31, "signal_c": 0.72}


class FakeFinishReason:
    """google-genai returns an enum whose `.name` carries the reason."""

    def __init__(self, name: str) -> None:
        self.name = name


class FakeCandidate:
    def __init__(self, reason: object) -> None:
        self.finish_reason = reason


class FakeResponse:
    def __init__(self, text: str = '{"score": 0.5}', reason: object | None = None) -> None:
        self.text = text
        self.candidates = [FakeCandidate(reason or FakeFinishReason("STOP"))]


class FakeModels:
    def __init__(self, response: object) -> None:
        self._response = response
        self.last_kwargs: dict = {}
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return self._response


class FakeClient:
    def __init__(self, response: object) -> None:
        self.models = FakeModels(response)


class TestEncode:
    def test_the_prompt_carries_every_feature(self) -> None:
        prompt = GeminiTriageCodec().encode(CASE)
        for name in CASE:
            assert name in prompt

    def test_values_are_written_at_full_precision(self) -> None:
        # Rounding in the prompt would silently blunt the intervention: the whole protocol
        # rests on the difference between a feature's real value and its neutral one.
        prompt = GeminiTriageCodec().encode({**CASE, "urgency_marker": 0.123456})
        assert "0.123456" in prompt

    def test_it_asks_for_a_continuous_score(self) -> None:
        prompt = GeminiTriageCodec().encode(CASE)
        assert "score" in prompt and "float" in prompt


class TestDecode:
    def setup_method(self) -> None:
        self.codec = GeminiTriageCodec()

    def test_plain_json(self) -> None:
        out = self.codec.decode(
            '{"score": 0.83, "decision": "HIGH_PRIORITY", "explanation": "urgency led"}'
        )
        assert out.score == 0.83
        assert out.decision == "HIGH_PRIORITY"
        assert out.explanation == "urgency led"

    def test_json_inside_a_code_fence(self) -> None:
        # Models wrap JSON in fences even when told not to.
        out = self.codec.decode('```json\n{"score": 0.4}\n```')
        assert out.score == 0.4

    def test_json_buried_in_prose(self) -> None:
        out = self.codec.decode('Sure! Here is the result: {"score": 0.61} Hope that helps.')
        assert out.score == 0.61

    def test_an_integer_score_is_accepted(self) -> None:
        assert self.codec.decode('{"score": 1}').score == 1.0

    def test_a_string_number_is_accepted(self) -> None:
        # Real models emit "0.42" as often as 0.42.
        assert self.codec.decode('{"score": "0.42"}').score == 0.42

    def test_missing_decision_and_explanation_get_defaults(self) -> None:
        out = self.codec.decode('{"score": 0.5}')
        assert out.decision == "UNKNOWN"
        assert out.explanation == ""

    def test_an_empty_response_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="empty response"):
            self.codec.decode("")

    def test_a_none_response_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="empty response"):
            self.codec.decode(None)

    def test_prose_with_no_json_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="no JSON object"):
            self.codec.decode("I am unable to score this case.")

    def test_malformed_json_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="malformed JSON"):
            self.codec.decode('{"score": 0.5,,,}')

    def test_a_hard_label_without_a_score_is_rejected(self) -> None:
        """The most important rejection in this file.

        The protocol measures *how far* a decision moved. A response carrying only a class
        label would collapse every delta to 0 or +/-1 and turn reproducibility into noise -
        so this fails loudly rather than falling back to the label.
        """
        with pytest.raises(ValidationError, match="no 'score' field"):
            self.codec.decode('{"decision": "HIGH_PRIORITY", "explanation": "urgency"}')

    def test_a_non_numeric_score_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not a number"):
            self.codec.decode('{"score": "very high"}')

    def test_a_null_score_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not a number"):
            self.codec.decode('{"score": null}')


class TestTransport:
    def test_a_normal_call_returns_text(self) -> None:
        transport = VertexGeminiTransport(
            project="p", client=FakeClient(FakeResponse('{"score":0.5}'))
        )
        assert transport.send("prompt", timeout=5.0) == '{"score":0.5}'
        assert transport.calls == 1

    def test_thinking_is_disabled_by_default(self) -> None:
        """Gemini 2.5 spends output tokens on internal reasoning before answering.

        The first real call against this model hit MAX_TOKENS at 256 for exactly that
        reason. A scoring function needs no reasoning tokens, and disabling them is cheaper,
        faster, and reduces the internal sampling that shows up as measured noise.
        """
        client = FakeClient(FakeResponse())
        VertexGeminiTransport(project="p", client=client).send("prompt", timeout=5.0)
        assert client.models.last_kwargs["config"]["thinking_config"] == {"thinking_budget": 0}

    def test_it_requests_json_at_temperature_zero(self) -> None:
        client = FakeClient(FakeResponse())
        VertexGeminiTransport(project="p", client=client).send("prompt", timeout=5.0)
        config = client.models.last_kwargs["config"]
        assert config["response_mime_type"] == "application/json"
        assert config["temperature"] == 0.0

    def test_truncation_is_named_rather_than_misdiagnosed(self) -> None:
        # Truncated JSON is indistinguishable from malformed JSON to a parser; without this
        # the failure reads as a prompt bug instead of a token-budget one.
        transport = VertexGeminiTransport(
            project="p",
            client=FakeClient(FakeResponse('{"score": 0.', FakeFinishReason("MAX_TOKENS"))),
        )
        with pytest.raises(ValidationError, match="max_output_tokens"):
            transport.send("prompt", timeout=5.0)

    def test_finish_reasons_are_counted_for_every_call(self) -> None:
        transport = VertexGeminiTransport(project="p", client=FakeClient(FakeResponse()))
        for _ in range(3):
            transport.send("prompt", timeout=5.0)
        assert transport.finish_reasons == {"STOP": 3}

    def test_a_response_with_no_candidates_is_recorded(self) -> None:
        class NoCandidates:
            text = ""
            candidates: list = []

        transport = VertexGeminiTransport(project="p", client=FakeClient(NoCandidates()))
        transport.send("prompt", timeout=5.0)
        assert transport.finish_reasons == {"NONE": 1}

    def test_a_plain_string_finish_reason_is_handled(self) -> None:
        # Not every SDK version returns an enum.
        transport = VertexGeminiTransport(
            project="p", client=FakeClient(FakeResponse('{"score":0.5}', "STOP"))
        )
        transport.send("prompt", timeout=5.0)
        assert transport.finish_reasons == {"STOP": 1}


class TestAssembly:
    def test_it_is_declared_non_deterministic(self) -> None:
        """A hosted LLM is not a pure function even at temperature 0.

        This declaration is load-bearing: it is what makes CachingTargetModel refuse to wrap
        the model, which prevents a cache from reporting perfect stability for a model whose
        measured spread reached 0.165.
        """
        model, _ = build_gemini_target(project="p")
        assert model.identity.deterministic is False

    def test_vendor_model_name_and_scope_version_stay_separate(self) -> None:
        # Conflating them would either break VersionScope's semver validation or smuggle a
        # vendor string into the field identifying what a verdict is true *of*.
        model, transport = build_gemini_target(
            project="p", gemini_model="gemini-2.5-flash", scope_version="2.0.0"
        )
        assert model.version == "2.0.0"
        assert transport.model == "gemini-2.5-flash"
        assert "gemini-2.5-flash" in model.model_id

    def test_it_defaults_to_the_laboratory_feature_space(self) -> None:
        model, _ = build_gemini_target(project="p")
        assert set(model.feature_space) == {"urgency_marker", "signal_b", "signal_c"}

    def test_end_to_end_through_a_fake_client(self) -> None:
        model, transport = build_gemini_target(project="p")
        transport.client = FakeClient(
            FakeResponse('{"score": 0.77, "decision": "HIGH_PRIORITY", "explanation": "urgency"}')
        )
        out = model.predict(dict(CASE))
        assert out.score == 0.77
        assert out.model_version == "1.0.0"  # Ariadne's scope, not the vendor's
