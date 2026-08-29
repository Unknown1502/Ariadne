"""Connecting Ariadne to a real, remote target model.

`docs/limitations.md` states the constraint plainly: the laboratory model is a hand-written
formula, and a real integration has to survive network failure, per-call cost, and a model
that does not answer identically twice. This module is the seam that makes that integration
tractable without weakening any of the protocol's guarantees.

**Nothing here changes what a verdict means.** The `TargetModel` protocol is unchanged, and
the experiment engine cannot tell a remote model from the synthetic one — which is the whole
point. Everything below is about making a remote model *satisfy* that existing contract
honestly, and refusing to run when it cannot.

The layering, outermost first:

    BudgetedTargetModel      hard cap on calls/spend; fails closed, never truncates
      CachingTargetModel     content-addressed reuse; ONLY legal for deterministic models
        ReplicatedTargetModel  averages n calls to pull a stochastic model's noise floor
          RemoteTargetModel      retries, timeouts, validation
            Transport            the actual network call
            FeatureCodec         numeric feature vector <-> the model's native request

Each layer satisfies `TargetModel`, so they compose in any order the integrator needs — but
the order above is the one that is correct, and `build_remote_model` assembles it for you.

## The three constraints, and what this module actually does about each

**1. The feature space is the real integration cost, and it cannot be automated away.**
Ariadne's protocol is "neutralize X while preserving Y, and see whether the decision moves."
That sentence is only meaningful over a structured feature space with a declared neutral
value per feature. A model taking free text has no such space, so a `FeatureCodec` is
mandatory and must be written by someone who knows the domain: they decide what the features
are, what neutral means for each, and how a feature vector becomes a request. This module
makes that contract explicit and checkable rather than pretending it is free.

**2. Cost is bounded by construction, not by hope.** A default investigation is 24 cases x 3
arms + 2 replicate probes = 74 calls. `BudgetedTargetModel` caps that and raises
`BudgetExhausted` rather than silently shortening the experiment, and `CachingTargetModel`
removes the duplicate calls the paired design inherently produces.

**3. Non-determinism is measured, not assumed away.** The verifier already refuses to issue a
verdict when a model disagrees with itself beyond `instability_threshold`. For a stochastic
model that gate would simply always fire — so `measure_noise_floor` samples the model's own
self-disagreement, and `replicates_needed` says how many calls must be averaged for the
resulting noise floor to sit far enough below the effect the claim predicts. That converts
"this model is noisy" from a blocker into a declared, priced sample-size decision.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from statistics import fmean, pstdev
from typing import Any, Protocol, runtime_checkable

from backend.core.errors import (
    BudgetExhausted,
    TargetModelError,
    ValidationError,
)
from backend.core.hashing import sha256_hex
from backend.experiment_engine.distributions import FeatureSpec
from backend.experiment_engine.target_model import TargetModel, TargetOutput

# --------------------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    """What a remote model is, and the one property that governs how it may be used.

    ``deterministic`` is not documentation. It gates caching (unsound for a stochastic
    model, because a cache would serve one sample forever and hide the very variance the
    verifier needs to see) and it is the integrator's explicit assertion, not something this
    code can verify for them — though `measure_noise_floor` will contradict them if they are
    wrong, which is exactly why it exists.
    """

    model_id: str
    version: str
    distribution_version: str
    deterministic: bool

    def __post_init__(self) -> None:
        for name, value in (
            ("model_id", self.model_id),
            ("version", self.version),
            ("distribution_version", self.distribution_version),
        ):
            if not value or not str(value).strip():
                raise ValidationError(f"ModelIdentity.{name} must be a non-empty string")


# --------------------------------------------------------------------------------------
# The two pieces an integrator writes
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawPrediction:
    """What a codec pulls out of a model's response, before Ariadne adds its own metadata."""

    score: float
    decision: str
    explanation: str


@runtime_checkable
class FeatureCodec(Protocol):
    """Translates between Ariadne's feature vector and one specific model's wire format.

    This is the domain-knowledge boundary and the honest cost of integration. `encode` turns
    ``{"urgency_marker": 0.5, ...}`` into whatever that model actually accepts — a JSON body,
    a prompt, a feature row. `decode` pulls a comparable score back out.

    The score must be **continuous and comparable across calls**: the entire protocol
    measures *how far* a decision moved, so a codec that returns only a hard label throws
    away the signal the verifier reasons about. If the model only emits a class, decode its
    probability/logit for the class of interest instead.
    """

    def encode(self, features: Mapping[str, float]) -> Any: ...

    def decode(self, payload: Any) -> RawPrediction: ...


@runtime_checkable
class Transport(Protocol):
    """Performs one call. Separated from the codec so reliability is testable without a network.

    Implementations should raise on failure and let `RemoteTargetModel` classify and retry;
    they should not implement their own retry loop, or the two will multiply.
    """

    def send(self, request: Any, *, timeout: float) -> Any: ...


# --------------------------------------------------------------------------------------
# Feature-space contract
# --------------------------------------------------------------------------------------


def validate_against_space(
    features: Mapping[str, float], space: Mapping[str, FeatureSpec]
) -> None:
    """Reject a feature vector the declared space cannot describe.

    The same strictness the synthetic laboratory applies, but against an integrator-supplied
    space rather than the lab's own. An unknown or out-of-range feature means the caller and
    the model disagree about the input space, and a score computed under that disagreement
    is meaningless while still looking like a number.
    """
    missing = [name for name in space if name not in features]
    if missing:
        raise ValidationError(f"feature vector is missing {sorted(missing)}")
    unknown = [name for name in features if name not in space]
    if unknown:
        raise ValidationError(f"feature vector contains undeclared features {sorted(unknown)}")
    for name, value in features.items():
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise ValidationError(f"feature {name!r} must be numeric, got {type(value).__name__}")
        spec = space[name]
        if not spec.contains(float(value)):
            raise ValidationError(
                f"feature {name!r}={value} is outside its declared range "
                f"[{spec.minimum}, {spec.maximum}]"
            )


# --------------------------------------------------------------------------------------
# The base adapter
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retry for genuinely transient remote failures.

    Deliberately small. A target model that needs many retries to answer is a model whose
    measurements should be distrusted, not one that should be hammered until it agrees.
    """

    max_attempts: int = 3
    base_delay_seconds: float = 0.2
    max_delay_seconds: float = 2.0

    def delay_for(self, attempt: int) -> float:
        return min(self.max_delay_seconds, self.base_delay_seconds * (2 ** (attempt - 1)))


class RemoteTargetModel:
    """A remote model, presented to the engine as an ordinary `TargetModel`.

    Validates the feature vector against the declared space, encodes it, sends it with a
    bounded retry, decodes the response, and returns a `TargetOutput` carrying Ariadne's own
    version metadata rather than anything the remote service claims about itself — the scope
    on a verdict must come from what Ariadne asked for, not from what a service self-reports.
    """

    def __init__(
        self,
        *,
        identity: ModelIdentity,
        codec: FeatureCodec,
        transport: Transport,
        feature_space: Mapping[str, FeatureSpec],
        retry: RetryPolicy | None = None,
        timeout_seconds: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not feature_space:
            raise ValidationError(
                "a remote target model needs a declared feature space; without neutral "
                "values per feature, 'neutralize this feature' has no defined meaning and "
                "the intervention protocol cannot be executed"
            )
        self.model_id = identity.model_id
        self.version = identity.version
        self.distribution_version = identity.distribution_version
        self.identity = identity
        self._codec = codec
        self._transport = transport
        self._space = dict(feature_space)
        self._retry = retry or RetryPolicy()
        self._timeout = timeout_seconds
        self._sleep = sleep

    @property
    def feature_space(self) -> Mapping[str, FeatureSpec]:
        return dict(self._space)

    def predict(self, features: dict[str, float]) -> TargetOutput:
        validate_against_space(features, self._space)
        request = self._codec.encode(features)

        last: Exception | None = None
        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                payload = self._transport.send(request, timeout=self._timeout)
                raw = self._codec.decode(payload)
                return self._to_output(raw)
            except ValidationError:
                # A malformed response is a contract violation, not a transient fault.
                # Retrying an identical request would produce an identical violation.
                raise
            except Exception as exc:  # noqa: BLE001 - classified and re-raised below
                last = exc
                if attempt < self._retry.max_attempts:
                    self._sleep(self._retry.delay_for(attempt))

        raise TargetModelError(
            f"{self.model_id}@{self.version} failed after "
            f"{self._retry.max_attempts} attempts: {last}"
        ) from last

    def _to_output(self, raw: RawPrediction) -> TargetOutput:
        score = float(raw.score)
        if score != score or score in (float("inf"), float("-inf")):
            raise ValidationError(
                f"{self.model_id} returned a non-finite score ({raw.score!r}); a "
                f"non-finite measurement cannot enter the evidence ledger"
            )
        return TargetOutput(
            decision=raw.decision,
            score=score,
            explanation=raw.explanation,
            model_id=self.model_id,
            model_version=self.version,
            distribution_version=self.distribution_version,
        )


# --------------------------------------------------------------------------------------
# Composable policy layers
# --------------------------------------------------------------------------------------


@dataclass
class CallLedger:
    """Observable record of what an experiment actually cost."""

    calls: int = 0
    cache_hits: int = 0
    spend: float = 0.0

    @property
    def billable_calls(self) -> int:
        return self.calls


class CachingTargetModel:
    """Content-addressed reuse of predictions.

    **Only sound for a deterministic model, and this refuses to wrap a stochastic one.**
    `predict` is supposed to be a pure function of (features, model version, distribution),
    so for a deterministic model a cache is invisible — the same key genuinely cannot produce
    a different answer. For a stochastic model it is actively harmful: the runner's
    instability probe calls the model twice with identical input specifically to observe
    disagreement, and a cache would return the first sample twice and report perfect
    stability for a model that has none. That is a silent false negative on the exact check
    that protects verdict integrity, so it is prevented here rather than documented as a
    caveat.

    The saving is real: a paired baseline/intervention/control design re-tests the same
    baseline cases across arms, so a meaningful fraction of calls are exact repeats.
    """

    def __init__(self, inner: TargetModel, *, ledger: CallLedger | None = None) -> None:
        deterministic = getattr(getattr(inner, "identity", None), "deterministic", None)
        if deterministic is False:
            raise ValidationError(
                "refusing to cache a model declared non-deterministic: caching would serve "
                "one sample repeatedly and make the instability probe report perfect "
                "stability for a model that has none. Wrap it in ReplicatedTargetModel "
                "instead, or correct the identity if the model really is deterministic."
            )
        self._inner = inner
        self.model_id = inner.model_id
        self.version = inner.version
        self.distribution_version = inner.distribution_version
        self.identity = getattr(inner, "identity", None)
        self._cache: dict[str, TargetOutput] = {}
        self.ledger = ledger or CallLedger()

    def cache_key(self, features: Mapping[str, float]) -> str:
        return sha256_hex(
            {
                "model_id": self.model_id,
                "version": self.version,
                "distribution": self.distribution_version,
                "features": dict(features),
            }
        )

    def predict(self, features: dict[str, float]) -> TargetOutput:
        key = self.cache_key(features)
        hit = self._cache.get(key)
        if hit is not None:
            self.ledger.cache_hits += 1
            return hit
        out = self._inner.predict(features)
        self._cache[key] = out
        return out


class BudgetedTargetModel:
    """A hard ceiling on calls and spend. Fails closed.

    Raises `BudgetExhausted` rather than returning a degraded result or quietly running
    fewer cases. An experiment that silently ran 9 of its declared 24 cases would produce
    evidence whose sample size contradicts its own plan, and the verifier would then apply
    a reproducibility threshold to a sample nobody authorised.
    """

    def __init__(
        self,
        inner: TargetModel,
        *,
        max_calls: int,
        cost_per_call: float = 0.0,
        max_spend: float | None = None,
        ledger: CallLedger | None = None,
    ) -> None:
        if max_calls < 1:
            raise ValidationError(f"max_calls must be at least 1, got {max_calls}")
        self._inner = inner
        self.model_id = inner.model_id
        self.version = inner.version
        self.distribution_version = inner.distribution_version
        self.identity = getattr(inner, "identity", None)
        self._max_calls = max_calls
        self._cost = cost_per_call
        self._max_spend = max_spend
        self.ledger = ledger or CallLedger()

    def predict(self, features: dict[str, float]) -> TargetOutput:
        if self.ledger.calls >= self._max_calls:
            raise BudgetExhausted(
                f"{self.model_id}@{self.version} hit its call budget "
                f"({self._max_calls}). Lower the plan's repetitions or raise the budget; "
                f"the experiment is not silently shortened."
            )
        projected = self.ledger.spend + self._cost
        if self._max_spend is not None and projected > self._max_spend:
            raise BudgetExhausted(
                f"{self.model_id}@{self.version} would exceed its spend cap "
                f"({projected:.4f} > {self._max_spend:.4f})."
            )
        out = self._inner.predict(features)
        self.ledger.calls += 1
        self.ledger.spend = projected
        return out

    @property
    def remaining_calls(self) -> int:
        return max(0, self._max_calls - self.ledger.calls)


class ReplicatedTargetModel:
    """Averages n calls per prediction, to pull a stochastic model's noise below the signal.

    The mean of n samples has standard error ``sd / sqrt(n)``, so replication is the lever
    that makes a noisy model measurable: it does not remove the noise, it prices it. Use
    `replicates_needed` to choose n from an actual measured noise floor rather than a guess,
    and note the cost is linear — n=9 means nine times the calls and nine times the bill.

    Deliberately still reports as non-deterministic. Averaging reduces variance; it does not
    make the model a pure function, and the instability probe should keep watching the
    averaged estimator rather than being told to stop looking.
    """

    def __init__(self, inner: TargetModel, *, replicates: int) -> None:
        if replicates < 1:
            raise ValidationError(f"replicates must be at least 1, got {replicates}")
        self._inner = inner
        self._n = replicates
        self.model_id = inner.model_id
        self.version = inner.version
        self.distribution_version = inner.distribution_version
        inner_identity = getattr(inner, "identity", None)
        self.identity = (
            ModelIdentity(
                model_id=inner_identity.model_id,
                version=inner_identity.version,
                distribution_version=inner_identity.distribution_version,
                deterministic=False,
            )
            if inner_identity is not None
            else None
        )

    @property
    def replicates(self) -> int:
        return self._n

    def predict(self, features: dict[str, float]) -> TargetOutput:
        outputs = [self._inner.predict(dict(features)) for _ in range(self._n)]
        mean_score = fmean(out.score for out in outputs)
        # Decision and explanation come from the first sample rather than a vote: they are
        # descriptive metadata here, while `score` is the measured quantity the protocol
        # actually reasons about, and inventing a "mean explanation" would be fiction.
        first = outputs[0]
        return TargetOutput(
            decision=first.decision,
            score=round(mean_score, 9),
            explanation=first.explanation,
            model_id=first.model_id,
            model_version=first.model_version,
            distribution_version=first.distribution_version,
        )


# --------------------------------------------------------------------------------------
# Measuring non-determinism instead of assuming it away
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NoiseProfile:
    """What a model's self-disagreement actually is, measured rather than declared."""

    samples: int
    cases: int
    mean_spread: float
    max_spread: float
    sd: float

    @property
    def looks_deterministic(self) -> bool:
        """True when repeated identical calls agreed to floating-point noise."""
        return self.max_spread <= 1e-9

    def suggested_instability_threshold(self) -> float:
        """A plan threshold derived from observed behaviour, with headroom.

        The verifier gates on ``instability > instability_threshold``. Setting that from a
        measurement rather than a guess is what stops a genuinely stable model being ruled
        INCONCLUSIVE by an over-tight default, or a noisy one sailing through an over-loose
        one.
        """
        return round(max(1e-9, self.max_spread * 1.5), 9)


def measure_noise_floor(
    model: TargetModel,
    cases: Sequence[Mapping[str, float]],
    *,
    samples: int = 5,
) -> NoiseProfile:
    """Call the model repeatedly on identical inputs and report how much it disagrees.

    Run this once when onboarding a model, before trusting any verdict from it. It answers
    two questions the integrator otherwise has to guess: is this model actually deterministic
    (and therefore cacheable), and how many replicates does a real effect need to clear its
    own noise?
    """
    if samples < 2:
        raise ValidationError("measuring self-disagreement needs at least 2 samples per case")
    if not cases:
        raise ValidationError("measuring a noise floor needs at least one case")

    spreads: list[float] = []
    sds: list[float] = []
    for case in cases:
        scores = [model.predict(dict(case)).score for _ in range(samples)]
        spreads.append(max(scores) - min(scores))
        sds.append(pstdev(scores) if len(scores) > 1 else 0.0)

    return NoiseProfile(
        samples=samples,
        cases=len(cases),
        mean_spread=round(fmean(spreads), 9),
        max_spread=round(max(spreads), 9),
        sd=round(fmean(sds), 9),
    )


def replicates_needed(
    noise_sd: float, *, min_effect_threshold: float, signal_to_noise: float = 4.0
) -> int:
    """How many averaged calls a real effect needs to stand clear of the model's own noise.

    The mean of n samples has standard error ``sd / sqrt(n)``, so requiring the effect
    threshold to be at least ``signal_to_noise`` standard errors gives::

        n >= (signal_to_noise * sd / threshold) ** 2

    A default of 4 standard errors is deliberately conservative: this decides whether an
    effect is real, and being wrong here means publishing a verdict that noise produced.
    Returns 1 for a deterministic model, and grows quadratically — which is precisely the
    cost signal an integrator needs before committing to a noisy model.
    """
    if min_effect_threshold <= 0:
        raise ValidationError("min_effect_threshold must be positive")
    if noise_sd <= 0:
        return 1
    import math

    return max(1, math.ceil((signal_to_noise * noise_sd / min_effect_threshold) ** 2))


# --------------------------------------------------------------------------------------
# Reference transport
# --------------------------------------------------------------------------------------


@dataclass
class HttpTransport:
    """A reference REST transport built on httpx.

    Kept deliberately thin — no retry (that is `RemoteTargetModel`'s job, and two retry
    loops multiply), no response interpretation (that is the codec's). Most integrations
    will replace this with their own SDK client; it exists to make the wiring concrete and
    to give the tests something real to substitute for.
    """

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    client: Any = None

    def send(self, request: Any, *, timeout: float) -> Any:
        client = self.client
        if client is None:
            import httpx

            client = httpx.Client()
            self.client = client
        response = client.post(self.url, json=request, headers=self.headers, timeout=timeout)
        response.raise_for_status()
        return response.json()


# --------------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------------


def build_remote_model(
    *,
    identity: ModelIdentity,
    codec: FeatureCodec,
    transport: Transport,
    feature_space: Mapping[str, FeatureSpec],
    max_calls: int,
    cost_per_call: float = 0.0,
    max_spend: float | None = None,
    replicates: int = 1,
    retry: RetryPolicy | None = None,
    timeout_seconds: float = 10.0,
    ledger: CallLedger | None = None,
) -> TargetModel:
    """Compose the layers in the one order that is correct.

    Budget outermost so it counts every real call including replicates; cache inside it so
    hits cost nothing and are not billed; replication innermost so it is what actually talks
    to the network. Caching is applied only for a model declared deterministic — for anything
    else it is silently skipped rather than quietly corrupting the instability probe.
    """
    shared = ledger or CallLedger()

    model: TargetModel = RemoteTargetModel(
        identity=identity,
        codec=codec,
        transport=transport,
        feature_space=feature_space,
        retry=retry,
        timeout_seconds=timeout_seconds,
    )

    if replicates > 1:
        model = ReplicatedTargetModel(model, replicates=replicates)

    if identity.deterministic and replicates == 1:
        model = CachingTargetModel(model, ledger=shared)

    return BudgetedTargetModel(
        model,
        max_calls=max_calls,
        cost_per_call=cost_per_call,
        max_spend=max_spend,
        ledger=shared,
    )
