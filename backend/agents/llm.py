"""Semantic reasoning: Gemini, and a deterministic offline stand-in.

Gemini does the genuinely ambiguous work - reading a sentence a person wrote and deciding
what testable prediction it makes. That is the part deterministic code is bad at, and it is
the only part an LLM is trusted with here.

`OfflineReasoner` exists because the scientific core must be verifiable without a Google
Cloud account or an API key. It is a rule-based parser, and the code is careful never to
pretend otherwise:

  - its model name is ``offline-deterministic-reasoner/1.0.0``, which is what lands in the
    provenance of anything it produces;
  - ``is_language_model`` is False, so the console can label a run honestly rather than
    showing a Gemini badge over a regex;
  - it handles the demo's claim shapes and openly returns low testability for anything else.

It is a test fixture and an offline fallback. Calling it an agent would be exactly the kind
of overclaim this project exists to argue against.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from backend.core.errors import AgentOutputError, AgentResponseRejected, AgentTimeout

# Rough public list-price figures used only for a cost *estimate* in observability output.
# Ariadne never presents these as billing data.
GEMINI_PRICE_PER_MILLION_INPUT = 0.30
GEMINI_PRICE_PER_MILLION_OUTPUT = 2.50


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """One completion, with the metadata the audit trail needs."""

    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    finish_reason: str = "stop"

    def estimated_cost_usd(self) -> float:
        return round(
            self.prompt_tokens / 1_000_000 * GEMINI_PRICE_PER_MILLION_INPUT
            + self.completion_tokens / 1_000_000 * GEMINI_PRICE_PER_MILLION_OUTPUT,
            8,
        )


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """A structured-output request."""

    system: str
    user: str
    task: str
    """Names the reasoning job (compile_claim, plan_experiment, recommend_action). The
    offline reasoner dispatches on it; Gemini ignores it."""

    context: dict[str, Any] = field(default_factory=dict)
    temperature: float = 0.0
    max_output_tokens: int = 2048
    timeout_seconds: float = 30.0
    response_schema: dict[str, Any] | None = None


@runtime_checkable
class LLMClient(Protocol):
    model_name: str
    is_language_model: bool

    def generate(self, request: LLMRequest) -> LLMResponse: ...


# --------------------------------------------------------------------------------------
# JSON handling
# --------------------------------------------------------------------------------------


FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model response.

    Models wrap JSON in prose or fences even when told not to. Recovering from that is
    reasonable; *guessing* at malformed JSON is not, so anything that will not parse raises
    AgentOutputError and the caller retries or fails explicitly.
    """
    candidate = text.strip()
    if not candidate:
        raise AgentOutputError("model returned an empty response")

    fenced = FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    if not candidate.startswith("{"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise AgentOutputError(f"no JSON object found in response: {text[:200]!r}")
        candidate = candidate[start : end + 1]

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise AgentOutputError(f"model returned malformed JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise AgentOutputError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


# --------------------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------------------


class GeminiClient:
    """Vertex AI / Google AI Studio Gemini client.

    Imports the SDK lazily so the whole project stays installable and testable without the
    Google Cloud extras.
    """

    is_language_model = True

    def __init__(
        self,
        *,
        model: str = "gemini-2.5-flash",
        api_key: str = "",
        use_vertex: bool = False,
        project: str = "",
        location: str = "asia-south1",
        max_output_tokens: int = 2048,
        thinking_budget: int = 0,
    ) -> None:
        self.model_name = model
        self._max_output_tokens = max_output_tokens
        self._thinking_budget = thinking_budget
        self._api_key = api_key
        self._use_vertex = use_vertex
        self._project = project
        self._location = location
        self._client: Any | None = None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from google import genai  # type: ignore[import-not-found,attr-defined]
        except ImportError as exc:  # pragma: no cover - requires the gcp extra
            raise AgentOutputError(
                "google-genai is not installed; install the 'gcp' extra or set "
                "LLM_PROVIDER=stub to run offline"
            ) from exc

        if self._use_vertex:
            self._client = genai.Client(
                vertexai=True, project=self._project, location=self._location
            )
        else:
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def generate(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover - network
        client = self._ensure_client()
        started = time.perf_counter()
        config: dict[str, Any] = {
            "temperature": request.temperature,
            "max_output_tokens": min(request.max_output_tokens, self._max_output_tokens),
            "system_instruction": request.system,
            "response_mime_type": "application/json",
            # Gemini 2.5 spends output tokens on internal reasoning before answering, and
            # that spend comes out of max_output_tokens. A claim compiler emits a small
            # structured object; left enabled, thinking consumed the entire budget and every
            # live call failed with MAX_TOKENS before producing any JSON. The target-model
            # adapter has always set this; the agent client had drifted without it.
            "thinking_config": {"thinking_budget": self._thinking_budget},
        }
        if request.response_schema:
            config["response_schema"] = request.response_schema

        try:
            response = client.models.generate_content(
                model=self.model_name, contents=request.user, config=config
            )
        except Exception as exc:
            message = str(exc).lower()
            if "deadline" in message or "timeout" in message:
                raise AgentTimeout(f"Gemini timed out: {exc}") from exc
            raise AgentOutputError(f"Gemini call failed: {exc}") from exc

        latency_ms = (time.perf_counter() - started) * 1000.0
        finish_reason = self._check_usable(response)
        usage = getattr(response, "usage_metadata", None)
        return LLMResponse(
            text=getattr(response, "text", "") or "",
            model=self.model_name,
            prompt_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            completion_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
            latency_ms=round(latency_ms, 3),
            finish_reason=finish_reason,
        )

    @staticmethod
    def _check_usable(response: Any) -> str:
        """Reject responses that only *look* like malformed JSON, and report the real cause.

        `finish_reason` lives on the candidate, not the response, so it is easy to never read
        - and the failure it signals is invisible until it happens. A response truncated at
        `max_output_tokens` arrives as syntactically broken JSON; without this check the
        parser calls it malformed, the caller retries, temperature-0 reproduces the identical
        truncation, and the loop budget drains against an error that was never transient.

        Returned rather than raised when the response is fine, so the finish reason reaches
        the audit trail either way.
        """
        feedback = getattr(response, "prompt_feedback", None)
        block_reason = getattr(feedback, "block_reason", None)
        if block_reason:
            raise AgentResponseRejected(
                f"Gemini blocked the prompt ({block_reason}). The explanation text is "
                f"attacker-controlled, so this is a plausible input, not a bug - the claim "
                f"should be quarantined rather than retried."
            )

        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            raise AgentResponseRejected(
                "Gemini returned no candidates; there is no output to parse and a retry "
                "would send the identical request."
            )

        reason = getattr(candidates[0], "finish_reason", None)
        name = getattr(reason, "name", None) or str(reason or "STOP")
        if name.upper() in {"MAX_TOKENS", "MAX_TOKEN"}:
            raise AgentResponseRejected(
                "Gemini hit max_output_tokens, so the JSON is cut off mid-structure. This "
                "is a configuration problem, not a flaky call: raise "
                "GEMINI_MAX_OUTPUT_TOKENS or shorten the prompt."
            )
        if name.upper() in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT"}:
            raise AgentResponseRejected(
                f"Gemini stopped generating for reason {name}; the response is incomplete "
                f"and an identical request will stop the same way."
            )
        return name


# --------------------------------------------------------------------------------------
# Offline reasoner
# --------------------------------------------------------------------------------------


PRIMACY_WORDS = (
    "primary", "main", "chief", "dominant", "biggest", "strongest", "principal",
    "key driver", "most important", "leading",
)
INCREASE_WORDS = ("increase", "raise", "boost", "elevate", "higher", "drove up", "pushed up")
DECREASE_WORDS = ("decrease", "lower", "reduce", "drop", "diminish", "pushed down")
VAGUE_WORDS = ("various", "several", "multiple factors", "complex", "holistic", "overall")

KNOWN_FEATURES = ("urgency_marker", "signal_b", "signal_c")
FEATURE_SYNONYMS = {
    "urgency marker": "urgency_marker",
    "urgency": "urgency_marker",
    "urgency indicator": "urgency_marker",
    "signal b": "signal_b",
    "signal c": "signal_c",
}


class OfflineReasoner:
    """A deterministic, rule-based stand-in for Gemini. Not a language model.

    Used by the test suite and by ``LLM_PROVIDER=stub`` so the scientific core runs with no
    network access. It parses the demo's explanation shapes and reports honest low
    testability for anything it does not recognize, rather than inventing structure.
    """

    model_name = "offline-deterministic-reasoner/1.0.0"
    is_language_model = False

    def __init__(self, *, fail_times: int = 0, malformed: bool = False, hang: bool = False):
        # Failure knobs let the chaos suite exercise the retry, malformed-output, and
        # timeout paths without patching internals.
        self._fail_times = fail_times
        self._malformed = malformed
        self._hang = hang
        self._calls = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self._calls += 1
        if self._hang:
            raise AgentTimeout(f"offline reasoner simulated a timeout on call {self._calls}")
        if self._calls <= self._fail_times:
            raise AgentOutputError(
                f"offline reasoner simulated a malformed response on call {self._calls}"
            )
        if self._malformed:
            return LLMResponse(text="{not json at all", model=self.model_name)

        match request.task:
            case "compile_claim":
                payload = self._compile_claim(request.context)
            case "plan_experiment":
                payload = self._plan_experiment(request.context)
            case "recommend_action":
                payload = self._recommend_action(request.context)
            case _:
                raise AgentOutputError(f"offline reasoner has no rule for task {request.task!r}")

        return LLMResponse(
            text=json.dumps(payload),
            model=self.model_name,
            prompt_tokens=len(request.user) // 4,
            completion_tokens=len(json.dumps(payload)) // 4,
            latency_ms=0.0,
        )

    # -- rules -------------------------------------------------------------------------

    def _compile_claim(self, context: dict[str, Any]) -> dict[str, Any]:
        explanation = str(context.get("explanation", ""))
        lowered = explanation.lower()

        subject = self._find_feature(lowered)
        is_primacy = any(word in lowered for word in PRIMACY_WORDS)
        is_vague = any(word in lowered for word in VAGUE_WORDS) or subject is None

        if any(word in lowered for word in INCREASE_WORDS) and not is_primacy:
            # "X raised the score" predicts that removing X lowers it.
            direction = "decrease"
        elif any(word in lowered for word in DECREASE_WORDS) and not is_primacy:
            direction = "increase"
        else:
            direction = "decrease"

        testability = 0.92 if (subject and is_primacy) else 0.55 if subject else 0.15
        ambiguities: list[str] = []
        if is_vague:
            ambiguities.append("the explanation does not name a single testable driver")
        if not is_primacy and subject:
            ambiguities.append("the explanation does not state whether the driver is primary")

        return {
            "subject": subject or "unspecified",
            "predicate": "is_primary_driver" if is_primacy else "influences",
            "object": "priority_score",
            "expected_direction": direction,
            "primacy_claim": is_primacy,
            "target_variables": [subject] if subject else [],
            "preserved_constraints": [f for f in KNOWN_FEATURES if f != subject],
            "assumptions": [
                "neutralizing a feature means setting it to its declared neutral value"
            ],
            "ambiguities": ambiguities,
            "testability_score": testability,
            "confidence": 0.80 if subject else 0.2,
        }

    def _plan_experiment(self, context: dict[str, Any]) -> dict[str, Any]:
        subject = str(context.get("subject", "urgency_marker"))
        # The control is the strongest competing feature the claim did not name.
        control = next(
            (f for f in ("signal_c", "signal_b", "urgency_marker") if f != subject), None
        )
        return {
            "intervention_type": "neutralize",
            "target_variable": subject,
            "intervention_value": 0.5,
            "control_variable": control,
            "control_value": 0.5,
            "preserved_features": [f for f in KNOWN_FEATURES if f not in (subject, control)],
            "repetitions": int(context.get("default_repetitions", 24)),
            "min_effect_threshold": 0.10,
            "confounders": [f for f in KNOWN_FEATURES if f != subject],
            "stopping_conditions": [
                "stop if the intervention violates a preserved constraint",
                "stop if the target model fails twice on the same case",
            ],
            "invalid_conditions": [
                "the intervention moves the target by less than 15% of its range",
                "any preserved feature changes beyond tolerance",
            ],
            "rationale": (
                f"Neutralize {subject} while holding other features fixed, and run "
                f"{control} as a control to test whether {subject} is the stronger driver."
            ),
        }

    def _recommend_action(self, context: dict[str, Any]) -> dict[str, Any]:
        debt = float(context.get("debt_total", 0.0))
        status = str(context.get("verdict_status", "") or "")
        contradictions = int(context.get("contradiction_count", 0))

        if debt >= 65 or contradictions >= 2:
            action = "REQUIRE_HUMAN_REVIEW"
        elif status == "CONTRADICTED":
            action = "INCREASE_AUDIT_PRIORITY"
        elif status == "INCONCLUSIVE":
            action = "SCHEDULE_REAUDIT"
        elif status == "SUPPORTED":
            action = "STORE_EVIDENCE"
        else:
            action = "NO_ACTION"

        return {
            "recommended_action": action,
            "rationale": (
                f"debt={debt:.1f}, verdict={status or 'none'}, "
                f"contradictions={contradictions}"
            ),
        }

    @staticmethod
    def _find_feature(lowered: str) -> str | None:
        for name in KNOWN_FEATURES:
            if name in lowered:
                return name
        for phrase, name in FEATURE_SYNONYMS.items():
            if phrase in lowered:
                return name
        return None


def build_llm_client(settings: Any | None = None) -> LLMClient:
    """Construct the configured reasoner. Defaults to offline."""
    from backend.config import get_settings

    config = settings or get_settings()
    if config.llm_provider == "gemini":
        return GeminiClient(
            model=config.gemini_model,
            api_key=config.google_api_key,
            use_vertex=config.use_vertex_ai,
            project=config.gcp_project_id,
            location=config.gcp_region,
            max_output_tokens=config.gemini_max_output_tokens,
        )
    return OfflineReasoner()
