"""A real third-party target model: Gemini, scoring triage cases through Vertex AI.

This is the reference integration `docs/integrating-a-real-model.md` describes, built
against a model nobody here wrote and nobody here can see inside. It exists to answer the
question the synthetic laboratory structurally cannot: does the adapter layer hold up
against a real remote model that is stochastic, billed per call, and genuinely opaque?

**What makes this a real test rather than a demo.** In the synthetic lab the answer is known
in advance — the formula is printed in the source, so a verdict can be checked by hand. Here
it is not. Gemini is asked to score a case *and* to say which input drove that score, and
Ariadne then tests whether that stated explanation survives a controlled intervention. Nobody
involved knows the answer before the experiment runs. That is the first time in this codebase
that has been true, and it is the whole point.

**Why this model is declared non-deterministic.** Even at temperature 0, a hosted LLM does not
guarantee identical output for identical input — batching, routing, and model updates all
perturb it. Declaring `deterministic=False` is what makes `CachingTargetModel` refuse to wrap
it, which is correct: caching would serve one sample forever and report perfect stability for
a model whose actual variance is the thing most worth measuring. Use
`measure_noise_floor` (see `backend/scripts/probe_real_model.py`) to find the real number
rather than assuming one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from backend.core.errors import ValidationError
from backend.experiment_engine.adapters import (
    ModelIdentity,
    RawPrediction,
    RemoteTargetModel,
    RetryPolicy,
)
from backend.experiment_engine.distributions import FEATURE_INDEX, FeatureSpec

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_LOCATION = "us-central1"

SYSTEM_INSTRUCTION = (
    "You are a triage prioritisation model in a synthetic laboratory. You receive three "
    "normalised signals and return a single priority score. You are a scoring function: "
    "given identical inputs you should return an identical score. Respond with JSON only."
)

PROMPT_TEMPLATE = """Score this case.

Signals (each normalised to 0.0-1.0):
  urgency_marker: {urgency_marker:.6f}
  signal_b:       {signal_b:.6f}
  signal_c:       {signal_c:.6f}

Return ONLY a JSON object, no prose and no code fence:
{{"score": <float 0.0-1.0>, "decision": "HIGH_PRIORITY" or "STANDARD_PRIORITY",
  "explanation": "<one short sentence naming the signal that drove the score most>"}}

Use HIGH_PRIORITY when score >= 0.60."""

FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class GeminiTriageCodec:
    """Turns a feature vector into a prompt, and the response back into a score.

    The score has to be continuous for the protocol to mean anything — the verifier measures
    *how far* a decision moved, so a codec returning only a hard label would collapse every
    delta to 0 or ±1 and turn reproducibility into noise. That is why the prompt asks for a
    float and this class refuses a response without one, rather than falling back to the
    label.
    """

    def encode(self, features: Any) -> str:
        return PROMPT_TEMPLATE.format(
            urgency_marker=float(features["urgency_marker"]),
            signal_b=float(features["signal_b"]),
            signal_c=float(features["signal_c"]),
        )

    def decode(self, payload: Any) -> RawPrediction:
        text = (payload or "").strip()
        if not text:
            raise ValidationError("Gemini returned an empty response")

        fenced = FENCE.search(text)
        if fenced:
            text = fenced.group(1).strip()
        if not text.startswith("{"):
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                raise ValidationError(f"no JSON object in Gemini response: {text[:160]!r}")
            text = text[start : end + 1]

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Gemini returned malformed JSON: {exc}") from exc

        if "score" not in parsed:
            raise ValidationError(
                f"Gemini response has no 'score' field: {sorted(parsed)}. A hard label alone "
                f"cannot be used - the protocol measures how far a score moved."
            )
        try:
            score = float(parsed["score"])
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"score is not a number: {parsed['score']!r}") from exc

        return RawPrediction(
            score=score,
            decision=str(parsed.get("decision", "UNKNOWN")),
            explanation=str(parsed.get("explanation", "")),
        )


@dataclass
class VertexGeminiTransport:
    """Calls Gemini on Vertex AI. One call, no retry — the adapter owns retry.

    Records `finish_reason` for every call. A response truncated at `max_output_tokens`
    arrives as JSON cut off mid-structure, which is indistinguishable from malformed JSON to
    a parser and would otherwise be misdiagnosed as a bad prompt.
    """

    project: str
    location: str = DEFAULT_LOCATION
    model: str = DEFAULT_MODEL
    temperature: float = 0.0
    max_output_tokens: int = 1024
    thinking_budget: int = 0
    client: Any = None
    finish_reasons: dict[str, int] = field(default_factory=dict)
    calls: int = 0

    def _ensure_client(self) -> Any:
        if self.client is None:
            from google import genai

            self.client = genai.Client(
                vertexai=True, project=self.project, location=self.location
            )
        return self.client

    def send(self, request: Any, *, timeout: float) -> Any:
        client = self._ensure_client()
        self.calls += 1
        response = client.models.generate_content(
            model=self.model,
            contents=request,
            config={
                "temperature": self.temperature,
                "max_output_tokens": self.max_output_tokens,
                "system_instruction": SYSTEM_INSTRUCTION,
                "response_mime_type": "application/json",
                # Gemini 2.5 is a thinking model: without this it spends output tokens on
                # internal reasoning before emitting an answer, and a scoring function does
                # not need reasoning tokens. Found the hard way - the first real call
                # against this model hit MAX_TOKENS at 256 because thinking consumed the
                # whole budget. Disabling it is cheaper, faster, and reduces the internal
                # sampling that shows up as noise in the measured score.
                "thinking_config": {"thinking_budget": self.thinking_budget},
            },
        )

        candidates = getattr(response, "candidates", None) or []
        reason = "NONE"
        if candidates:
            raw = getattr(candidates[0], "finish_reason", None)
            reason = getattr(raw, "name", None) or str(raw or "STOP")
        self.finish_reasons[reason] = self.finish_reasons.get(reason, 0) + 1

        if reason.upper() in {"MAX_TOKENS", "MAX_TOKEN"}:
            raise ValidationError(
                "Gemini hit max_output_tokens; the JSON is truncated. Raise "
                "max_output_tokens rather than treating this as a malformed response."
            )
        return getattr(response, "text", "") or ""


def build_gemini_target(
    *,
    project: str,
    gemini_model: str = DEFAULT_MODEL,
    scope_version: str = "1.0.0",
    distribution_version: str = "baseline_2024.1",
    location: str = DEFAULT_LOCATION,
    temperature: float = 0.0,
    feature_space: dict[str, FeatureSpec] | None = None,
    timeout_seconds: float = 30.0,
) -> tuple[RemoteTargetModel, VertexGeminiTransport]:
    """Build the bare remote model plus its transport, so callers can read call counts.

    `gemini_model` and `scope_version` are deliberately separate. The first is the vendor's
    model name (`gemini-2.5-flash`); the second is the semver Ariadne stamps on every piece
    of evidence, which `VersionScope` requires to be MAJOR.MINOR.PATCH. Conflating them
    would either break scope validation or smuggle a vendor string into the field that
    identifies what a verdict is true *of*.

    Returned unwrapped on purpose: budget, cache, and replication are the *caller's*
    decisions, made from a measured noise floor rather than guessed here. See
    `backend/scripts/probe_real_model.py`.
    """
    transport = VertexGeminiTransport(
        project=project, location=location, model=gemini_model, temperature=temperature
    )
    model = RemoteTargetModel(
        identity=ModelIdentity(
            model_id=f"gemini-triage/{gemini_model}",
            version=scope_version,
            distribution_version=distribution_version,
            deterministic=False,  # a hosted LLM is not a pure function, even at temperature 0
        ),
        codec=GeminiTriageCodec(),
        transport=transport,
        feature_space=feature_space or dict(FEATURE_INDEX),
        retry=RetryPolicy(max_attempts=3, base_delay_seconds=0.5, max_delay_seconds=4.0),
        timeout_seconds=timeout_seconds,
    )
    return model, transport
