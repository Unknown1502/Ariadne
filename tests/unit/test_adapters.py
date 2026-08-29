"""The remote-model adapter layer.

The load-bearing test in this file is `TestTransparentToTheProtocol` at the bottom: the same
synthetic v1.0.0 model, reached through the full remote adapter stack (codec, transport,
retry, cache, budget), must still produce CONTRADICTED with the same reason codes as the
in-process model does. If the adapter changed a verdict, it would be changing what Ariadne
measures rather than merely how it reaches the model — and every other test here would be
beside the point.
"""

from __future__ import annotations

import pytest

from backend.core.errors import BudgetExhausted, TargetModelError, ValidationError
from backend.experiment_engine.adapters import (
    BudgetedTargetModel,
    CachingTargetModel,
    HttpTransport,
    ModelIdentity,
    RawPrediction,
    RemoteTargetModel,
    ReplicatedTargetModel,
    RetryPolicy,
    build_remote_model,
    measure_noise_floor,
    replicates_needed,
    validate_against_space,
)
from backend.experiment_engine.distributions import FEATURE_INDEX, get_fixture_set
from backend.experiment_engine.target_model import (
    TargetModel,
    UnstableTriageModel,
    get_target_model,
)

SPACE = FEATURE_INDEX
CASE = {"urgency_marker": 0.8, "signal_b": 0.3, "signal_c": 0.7}


def identity(deterministic: bool = True, version: str = "1.0.0") -> ModelIdentity:
    return ModelIdentity(
        model_id="remote-triage",
        version=version,
        distribution_version="baseline_2024.1",
        deterministic=deterministic,
    )


class DictCodec:
    """A minimal codec: features go out as JSON, a score comes back."""

    def encode(self, features):
        return {"features": dict(features)}

    def decode(self, payload):
        return RawPrediction(
            score=payload["score"],
            decision=payload.get("decision", "HIGH_PRIORITY"),
            explanation=payload.get("explanation", "Urgency marker was the primary driver."),
        )


class ScriptedTransport:
    """Returns queued responses or raises queued exceptions, and records every call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[object, float]] = []

    def send(self, request, *, timeout):
        self.calls.append((request, timeout))
        if not self._responses:
            raise AssertionError("ScriptedTransport ran out of scripted responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class SyntheticTransport:
    """Serves a real synthetic laboratory model as if it were a remote service."""

    def __init__(self, version: str = "1.0.0"):
        self._model = get_target_model(version)
        self.calls = 0

    def send(self, request, *, timeout):
        self.calls += 1
        out = self._model.predict(dict(request["features"]))
        return {"score": out.score, "decision": out.decision, "explanation": out.explanation}


def remote(transport, *, det: bool = True, retry: RetryPolicy | None = None) -> RemoteTargetModel:
    return RemoteTargetModel(
        identity=identity(det),
        codec=DictCodec(),
        transport=transport,
        feature_space=SPACE,
        retry=retry,
        sleep=lambda _seconds: None,  # never actually sleep in tests
    )


class TestFeatureSpaceContract:
    def test_a_valid_vector_passes(self) -> None:
        validate_against_space(CASE, SPACE)

    def test_missing_feature_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="missing"):
            validate_against_space({"urgency_marker": 0.8}, SPACE)

    def test_undeclared_feature_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="undeclared"):
            validate_against_space({**CASE, "backdoor": 1.0}, SPACE)

    def test_out_of_range_value_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="outside its declared range"):
            validate_against_space({**CASE, "urgency_marker": 5.0}, SPACE)

    def test_non_numeric_value_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be numeric"):
            validate_against_space({**CASE, "urgency_marker": "high"}, SPACE)

    def test_a_model_without_a_feature_space_is_refused(self) -> None:
        # Without declared neutral values, "neutralize this feature" has no meaning.
        with pytest.raises(ValidationError, match="declared feature space"):
            RemoteTargetModel(
                identity=identity(),
                codec=DictCodec(),
                transport=ScriptedTransport([]),
                feature_space={},
            )


class TestRemoteTargetModel:
    def test_it_satisfies_the_target_model_protocol(self) -> None:
        assert isinstance(remote(ScriptedTransport([{"score": 0.5}])), TargetModel)

    def test_a_successful_call_round_trips(self) -> None:
        transport = ScriptedTransport([{"score": 0.62, "decision": "HIGH_PRIORITY"}])
        out = remote(transport).predict(dict(CASE))
        assert out.score == 0.62
        assert out.decision == "HIGH_PRIORITY"
        assert transport.calls[0][0] == {"features": CASE}

    def test_version_metadata_comes_from_ariadne_not_the_service(self) -> None:
        # A verdict's scope must reflect what Ariadne asked for. A service that
        # self-reports a different version must not be able to relabel the evidence.
        transport = ScriptedTransport([{"score": 0.5, "model_version": "9.9.9"}])
        out = remote(transport).predict(dict(CASE))
        assert out.model_version == "1.0.0"
        assert out.distribution_version == "baseline_2024.1"

    def test_a_transient_failure_is_retried(self) -> None:
        transport = ScriptedTransport([RuntimeError("503"), {"score": 0.44}])
        assert remote(transport).predict(dict(CASE)).score == 0.44
        assert len(transport.calls) == 2

    def test_retries_are_bounded_then_it_fails_loudly(self) -> None:
        transport = ScriptedTransport([RuntimeError("503")] * 3)
        with pytest.raises(TargetModelError, match="after 3 attempts"):
            remote(transport).predict(dict(CASE))
        assert len(transport.calls) == 3

    def test_a_malformed_response_is_not_retried(self) -> None:
        # A contract violation reproduces identically; retrying only wastes budget.
        class BadCodec(DictCodec):
            def decode(self, payload):
                raise ValidationError("response had no score field")

        transport = ScriptedTransport([{"nope": True}] * 3)
        model = RemoteTargetModel(
            identity=identity(),
            codec=BadCodec(),
            transport=transport,
            feature_space=SPACE,
            sleep=lambda _s: None,
        )
        with pytest.raises(ValidationError, match="no score field"):
            model.predict(dict(CASE))
        assert len(transport.calls) == 1

    def test_a_non_finite_score_never_reaches_the_ledger(self) -> None:
        transport = ScriptedTransport([{"score": float("nan")}])
        with pytest.raises(ValidationError, match="non-finite"):
            remote(transport).predict(dict(CASE))

    def test_backoff_grows_and_is_capped(self) -> None:
        policy = RetryPolicy(max_attempts=5, base_delay_seconds=0.1, max_delay_seconds=0.25)
        assert policy.delay_for(1) == 0.1
        assert policy.delay_for(2) == 0.2
        assert policy.delay_for(3) == 0.25
        assert policy.delay_for(9) == 0.25


class TestCaching:
    def test_a_repeat_call_is_served_from_cache(self) -> None:
        transport = SyntheticTransport()
        cached = CachingTargetModel(remote(transport))
        first = cached.predict(dict(CASE))
        second = cached.predict(dict(CASE))
        assert first == second
        assert transport.calls == 1
        assert cached.ledger.cache_hits == 1

    def test_different_features_are_different_keys(self) -> None:
        transport = SyntheticTransport()
        cached = CachingTargetModel(remote(transport))
        cached.predict(dict(CASE))
        cached.predict({**CASE, "urgency_marker": 0.5})
        assert transport.calls == 2

    def test_caching_a_stochastic_model_is_refused(self) -> None:
        """The whole point: a cache would make the instability probe lie.

        The runner calls the model twice on identical input specifically to catch a model
        that disagrees with itself. Cached, that check reports perfect stability for a
        model that has none - a silent false negative on the gate protecting verdict
        integrity.
        """
        with pytest.raises(ValidationError, match="non-deterministic"):
            CachingTargetModel(remote(ScriptedTransport([]), det=False))

    def test_the_refusal_is_not_theoretical(self) -> None:
        # Demonstrates the harm the guard prevents, using a genuinely unstable model.
        unstable = UnstableTriageModel()
        raw = {unstable.predict(dict(CASE)).score for _ in range(5)}
        assert len(raw) > 1, "the double really is unstable"


class TestBudget:
    def test_calls_are_counted(self) -> None:
        budgeted = BudgetedTargetModel(remote(SyntheticTransport()), max_calls=5)
        budgeted.predict(dict(CASE))
        assert budgeted.ledger.calls == 1
        assert budgeted.remaining_calls == 4

    def test_it_fails_closed_rather_than_shortening_the_experiment(self) -> None:
        budgeted = BudgetedTargetModel(remote(SyntheticTransport()), max_calls=2)
        budgeted.predict(dict(CASE))
        budgeted.predict(dict(CASE))
        with pytest.raises(BudgetExhausted, match="call budget"):
            budgeted.predict(dict(CASE))

    def test_a_spend_cap_is_enforced_before_the_call(self) -> None:
        budgeted = BudgetedTargetModel(
            remote(SyntheticTransport()), max_calls=100, cost_per_call=0.10, max_spend=0.25
        )
        for _ in range(2):
            budgeted.predict(dict(CASE))
        assert budgeted.ledger.spend == pytest.approx(0.20)
        with pytest.raises(BudgetExhausted, match="spend cap"):
            budgeted.predict(dict(CASE))

    def test_a_zero_budget_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValidationError, match="at least 1"):
            BudgetedTargetModel(remote(SyntheticTransport()), max_calls=0)


class TestReplication:
    def test_it_averages_the_score(self) -> None:
        transport = ScriptedTransport([{"score": 0.4}, {"score": 0.6}, {"score": 0.5}])
        replicated = ReplicatedTargetModel(remote(transport), replicates=3)
        assert replicated.predict(dict(CASE)).score == pytest.approx(0.5)
        assert len(transport.calls) == 3

    def test_averaging_does_not_relabel_the_model_as_deterministic(self) -> None:
        # Variance is reduced, not eliminated; the instability probe must keep watching.
        replicated = ReplicatedTargetModel(remote(ScriptedTransport([]), det=False), replicates=4)
        assert replicated.identity is not None
        assert replicated.identity.deterministic is False

    def test_replication_reduces_observed_spread(self) -> None:
        base = UnstableTriageModel(jitter=0.20)
        single = {round(base.predict(dict(CASE)).score, 6) for _ in range(8)}
        averaged = ReplicatedTargetModel(base, replicates=16)
        spread_single = max(single) - min(single)
        samples = [averaged.predict(dict(CASE)).score for _ in range(8)]
        assert (max(samples) - min(samples)) < spread_single

    def test_at_least_one_replicate_is_required(self) -> None:
        with pytest.raises(ValidationError, match="at least 1"):
            ReplicatedTargetModel(remote(ScriptedTransport([])), replicates=0)


class TestNoiseMeasurement:
    def test_a_deterministic_model_reports_no_noise(self) -> None:
        cases = [c.features for c in get_fixture_set("triage_baseline_v1").cases(3)]
        profile = measure_noise_floor(get_target_model("1.0.0"), cases, samples=4)
        assert profile.looks_deterministic
        assert profile.max_spread == 0.0

    def test_a_stochastic_model_is_caught(self) -> None:
        cases = [c.features for c in get_fixture_set("triage_baseline_v1").cases(3)]
        profile = measure_noise_floor(UnstableTriageModel(jitter=0.15), cases, samples=5)
        assert not profile.looks_deterministic
        assert profile.max_spread > 0.0
        assert profile.sd > 0.0

    def test_the_suggested_threshold_has_headroom_over_observed_noise(self) -> None:
        cases = [c.features for c in get_fixture_set("triage_baseline_v1").cases(2)]
        profile = measure_noise_floor(UnstableTriageModel(jitter=0.1), cases, samples=5)
        assert profile.suggested_instability_threshold() > profile.max_spread

    def test_measuring_needs_repeated_samples(self) -> None:
        with pytest.raises(ValidationError, match="at least 2 samples"):
            measure_noise_floor(get_target_model("1.0.0"), [CASE], samples=1)

    def test_measuring_needs_a_case(self) -> None:
        with pytest.raises(ValidationError, match="at least one case"):
            measure_noise_floor(get_target_model("1.0.0"), [], samples=3)


class TestReplicatesNeeded:
    def test_a_deterministic_model_needs_one_call(self) -> None:
        assert replicates_needed(0.0, min_effect_threshold=0.10) == 1

    def test_noise_at_the_threshold_demands_many_calls(self) -> None:
        # sd == threshold means the effect is one sd; 4 standard errors needs n = 16.
        assert replicates_needed(0.10, min_effect_threshold=0.10) == 16

    def test_the_requirement_grows_quadratically(self) -> None:
        # Doubling the noise quadruples the calls - the cost signal an integrator needs.
        assert replicates_needed(0.05, min_effect_threshold=0.10) == 4
        assert replicates_needed(0.10, min_effect_threshold=0.10) == 16

    def test_a_stricter_signal_to_noise_costs_more(self) -> None:
        loose = replicates_needed(0.05, min_effect_threshold=0.10, signal_to_noise=2.0)
        strict = replicates_needed(0.05, min_effect_threshold=0.10, signal_to_noise=4.0)
        assert strict > loose

    def test_a_nonpositive_threshold_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be positive"):
            replicates_needed(0.05, min_effect_threshold=0.0)


class TestAssembly:
    def test_a_deterministic_model_gets_a_cache(self) -> None:
        transport = SyntheticTransport()
        model = build_remote_model(
            identity=identity(deterministic=True),
            codec=DictCodec(),
            transport=transport,
            feature_space=SPACE,
            max_calls=50,
        )
        model.predict(dict(CASE))
        model.predict(dict(CASE))
        assert transport.calls == 1, "the second identical call should have been cached"

    def test_a_stochastic_model_is_never_cached(self) -> None:
        transport = SyntheticTransport()
        model = build_remote_model(
            identity=identity(deterministic=False),
            codec=DictCodec(),
            transport=transport,
            feature_space=SPACE,
            max_calls=50,
        )
        model.predict(dict(CASE))
        model.predict(dict(CASE))
        assert transport.calls == 2, "caching a stochastic model would hide its variance"

    def test_the_budget_counts_replicated_calls(self) -> None:
        transport = SyntheticTransport()
        model = build_remote_model(
            identity=identity(deterministic=False),
            codec=DictCodec(),
            transport=transport,
            feature_space=SPACE,
            max_calls=2,
            replicates=3,
        )
        model.predict(dict(CASE))
        model.predict(dict(CASE))
        with pytest.raises(BudgetExhausted):
            model.predict(dict(CASE))
        assert transport.calls == 6  # 2 predictions x 3 replicates

    def test_the_assembled_stack_is_still_a_target_model(self) -> None:
        model = build_remote_model(
            identity=identity(),
            codec=DictCodec(),
            transport=SyntheticTransport(),
            feature_space=SPACE,
            max_calls=10,
        )
        assert isinstance(model, TargetModel)


class TestHttpTransport:
    def test_it_posts_and_returns_json(self) -> None:
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"score": 0.71}

        class FakeClient:
            def __init__(self):
                self.seen = None

            def post(self, url, json, headers, timeout):
                self.seen = (url, json, headers, timeout)
                return FakeResponse()

        client = FakeClient()
        transport = HttpTransport(url="https://model.test/predict", client=client)
        assert transport.send({"features": CASE}, timeout=5.0) == {"score": 0.71}
        assert client.seen[0] == "https://model.test/predict"
        assert client.seen[3] == 5.0


class TestTransparentToTheProtocol:
    """The adapter must change how the model is reached, never what the verdict is."""

    def _verdict_through(self, model_factory):
        from backend.core.clock import ManualClock
        from backend.experiment_engine.runner import ExperimentRunner
        from backend.verifier.verifier import verify
        from tests.factories import T0, make_claim, make_plan

        claim = make_claim()
        plan = make_plan(claim)
        runner = ExperimentRunner(
            clock=ManualClock(T0), model_factory=lambda *_args, **_kw: model_factory()
        )
        result = runner.run(plan, claim)
        return verify(result.evidence, claim, plan)

    def test_the_same_model_reached_remotely_reaches_the_same_verdict(self) -> None:
        direct = self._verdict_through(lambda: get_target_model("1.0.0"))
        through_adapter = self._verdict_through(
            lambda: build_remote_model(
                identity=identity(deterministic=True),
                codec=DictCodec(),
                transport=SyntheticTransport("1.0.0"),
                feature_space=SPACE,
                max_calls=500,
            )
        )
        assert through_adapter.status == direct.status
        assert through_adapter.reason_codes == direct.reason_codes
        assert through_adapter.effect_size == pytest.approx(direct.effect_size)

    def test_a_budget_too_small_for_the_plan_fails_loudly(self) -> None:
        # Better to refuse than to publish evidence whose sample size contradicts its plan.
        with pytest.raises(BudgetExhausted):
            self._verdict_through(
                lambda: build_remote_model(
                    identity=identity(deterministic=True),
                    codec=DictCodec(),
                    transport=SyntheticTransport("1.0.0"),
                    feature_space=SPACE,
                    max_calls=5,
                )
            )
