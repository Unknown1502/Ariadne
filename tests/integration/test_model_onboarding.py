"""Onboarding a model, from nothing to verifiable, through the real API.

The question this file settles is the one an external ML team actually asks: *can I connect
my model to this, and will it tell me when I am done?* Everything here goes through the
production routes against real persistence - no fixtures reach into the store to set state
that a user would have had to configure.

Two properties matter more than the individual gates.

**Readiness is derived, not stored.** `RegisteredModel.status` caches the last answer; it is
never consulted to produce the next one. A model whose endpoint fails overnight must stop
being ready the next time anyone asks, and the test that proves it breaks a live connection
after reaching READY.

**Every blocker is actionable.** A gate that reports NOT READY without saying what to do is
a dead end, so each failing check is asserted to carry an imperative.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    from backend.config import reset_settings_cache

    previous = os.environ.get("VAR_DIR")
    os.environ["VAR_DIR"] = tempfile.mkdtemp()
    reset_settings_cache()
    try:
        from backend.api.main import app

        with TestClient(app) as test_client:
            yield test_client
    finally:
        if previous is None:
            os.environ.pop("VAR_DIR", None)
        else:
            os.environ["VAR_DIR"] = previous
        reset_settings_cache()


MODEL = {"model_id": "triage-model", "name": "Triage model", "provider": "laboratory"}
GOOD_RESPONSE = {"score": 0.83, "decision": "HIGH_PRIORITY", "explanation": "urgency drove it"}


def onboard(client: TestClient, *, complete: bool = True) -> str:
    """Walk the real onboarding path and return the registered model's id."""
    registered = client.post("/api/v1/registered-models", json=MODEL).json()
    model_id = registered["id"]
    if not complete:
        return model_id

    endpoint = client.post(
        "/api/v1/connections",
        json={
            "kind": "MODEL_ENDPOINT",
            "name": "Synthetic laboratory",
            "transport": "IN_PROCESS",
            "model_version": "2.0.0",
        },
    ).json()["id"]
    client.post(f"/api/v1/connections/{endpoint}/test")

    registry = client.post(
        "/api/v1/connections",
        json={"kind": "MODEL_REGISTRY", "name": "Registry events", "transport": "IN_PROCESS"},
    ).json()["id"]
    client.post(f"/api/v1/connections/{registry}/test")

    client.patch(f"/api/v1/registered-models/{model_id}/output", json={"score_path": "score"})
    client.post(
        f"/api/v1/registered-models/{model_id}/output/validate",
        json={"sample_response": GOOD_RESPONSE},
    )
    client.post(
        "/api/v1/feature-semantics",
        json={
            "model_id": "triage-model",
            "name": "urgency_marker",
            "data_type": "CONTINUOUS",
            "minimum": 0.0,
            "maximum": 1.0,
            "neutral_strategy": "EXPLICIT",
            "neutral_value": 0.5,
        },
    )
    client.post(
        "/api/v1/explanation-sources",
        json={
            "model_id": "triage-model",
            "name": "Model response",
            "source_type": "MODEL_RESPONSE",
        },
    )

    from backend.api.main import app_state

    runtime = app_state().runtime
    model = runtime.get_model(model_id)
    runtime.save_model(model.model_copy(update={"connection_id": endpoint}))
    return model_id


class TestRegistration:
    def test_a_new_model_is_configuring_not_ready(self, client: TestClient) -> None:
        """Registering a model does not make it verifiable, and must not say it does."""
        response = client.post("/api/v1/registered-models", json=MODEL)
        assert response.status_code == 201
        assert response.json()["status"] == "CONFIGURING"

    def test_registering_the_same_model_twice_is_refused(self, client: TestClient) -> None:
        client.post("/api/v1/registered-models", json=MODEL)
        assert client.post("/api/v1/registered-models", json=MODEL).status_code == 409

    def test_a_deleted_model_is_gone(self, client: TestClient) -> None:
        model_id = client.post("/api/v1/registered-models", json=MODEL).json()["id"]
        assert client.delete(f"/api/v1/registered-models/{model_id}").status_code == 204
        assert client.get(f"/api/v1/registered-models/{model_id}").status_code == 404


class TestTheOutputContract:
    def test_a_class_label_score_is_refused(self, client: TestClient) -> None:
        """The strictest check, and the one that saves the most money.

        The protocol measures how far a decision moved. A score that turns out to be a class
        label collapses every delta to nothing or a full unit and makes reproducibility
        noise - caught here it costs one request, caught later it costs an experiment.
        """
        model_id = onboard(client, complete=False)
        client.patch(f"/api/v1/registered-models/{model_id}/output", json={"score_path": "score"})
        result = client.post(
            f"/api/v1/registered-models/{model_id}/output/validate",
            json={"sample_response": {"score": "HIGH", "decision": "d", "explanation": "e"}},
        ).json()

        assert result["ok"] is False
        failure = next(check for check in result["checks"] if check["name"] == "score")
        assert "not a number" in failure["detail"]

    def test_a_missing_path_is_named(self, client: TestClient) -> None:
        model_id = onboard(client, complete=False)
        client.patch(
            f"/api/v1/registered-models/{model_id}/output",
            json={"score_path": "prediction.value"},
        )
        result = client.post(
            f"/api/v1/registered-models/{model_id}/output/validate",
            json={"sample_response": GOOD_RESPONSE},
        ).json()
        assert result["ok"] is False
        assert "prediction.value" in str(result["checks"])

    def test_a_nested_path_resolves(self, client: TestClient) -> None:
        """Real responses nest. The contract exists so shapes do not have to match ours."""
        model_id = onboard(client, complete=False)
        client.patch(
            f"/api/v1/registered-models/{model_id}/output",
            json={
                "score_path": "prediction.score",
                "decision_path": "prediction.label",
                "explanation_path": "meta.reason",
            },
        )
        result = client.post(
            f"/api/v1/registered-models/{model_id}/output/validate",
            json={
                "sample_response": {
                    "prediction": {"score": 0.7, "label": "HIGH"},
                    "meta": {"reason": "urgency drove it"},
                }
            },
        ).json()
        assert result["ok"] is True

    def test_declaring_a_path_does_not_validate_it(self, client: TestClient) -> None:
        """A plausible path is not a verified one, and readiness must know the difference."""
        model_id = onboard(client, complete=False)
        client.patch(f"/api/v1/registered-models/{model_id}/output", json={"score_path": "score"})
        readiness = client.get(f"/api/v1/registered-models/{model_id}/readiness").json()
        contract = next(c for c in readiness["checks"] if c["name"] == "OUTPUT_CONTRACT")
        assert contract["passed"] is False
        assert "never checked against a response" in contract["detail"]


class TestReadiness:
    def test_a_bare_model_reports_every_blocker(self, client: TestClient) -> None:
        model_id = onboard(client, complete=False)
        readiness = client.get(f"/api/v1/registered-models/{model_id}/readiness").json()

        assert readiness["ready"] is False
        assert readiness["status"] == "NOT_READY"
        failing = {c["name"] for c in readiness["checks"] if not c["passed"]}
        assert {
            "MODEL_ENDPOINT",
            "OUTPUT_CONTRACT",
            "EXPLANATION_SOURCE",
            "FEATURE_SEMANTICS",
            "LIFECYCLE_EVENTS",
        } <= failing

    def test_every_blocker_says_what_to_do(self, client: TestClient) -> None:
        """A gate that reports NOT READY without an imperative is a dead end."""
        model_id = onboard(client, complete=False)
        readiness = client.get(f"/api/v1/registered-models/{model_id}/readiness").json()
        for check in readiness["checks"]:
            if not check["passed"]:
                assert check["blocker"], f"{check['name']} fails without saying how to clear it"
                assert len(check["blocker"]) > 20

    def test_full_onboarding_reaches_ready(self, client: TestClient) -> None:
        """The journey an external team has to be able to complete."""
        model_id = onboard(client)
        readiness = client.get(f"/api/v1/registered-models/{model_id}/readiness").json()

        assert readiness["ready"] is True, [
            c["blocker"] for c in readiness["checks"] if not c["passed"]
        ]
        assert readiness["status"] == "READY_FOR_VERIFICATION"
        assert client.get(f"/api/v1/registered-models/{model_id}").json()["status"] == "READY"

    def test_readiness_is_derived_not_stored(self, client: TestClient) -> None:
        """The property that makes the label worth anything.

        A model that reached READY and whose endpoint then failed must stop being ready the
        next time anyone asks. A status that only changes when someone edits a form is a
        status that lies within a day.
        """
        model_id = onboard(client)
        assert client.get(f"/api/v1/registered-models/{model_id}/readiness").json()["ready"]

        # The endpoint goes down, exactly as it would at 3am.
        model = client.get(f"/api/v1/registered-models/{model_id}").json()
        client.post(f"/api/v1/connections/{model['connection_id']}/disable")

        after = client.get(f"/api/v1/registered-models/{model_id}/readiness").json()
        assert after["ready"] is False
        endpoint = next(c for c in after["checks"] if c["name"] == "MODEL_ENDPOINT")
        assert endpoint["passed"] is False
        assert client.get(f"/api/v1/registered-models/{model_id}").json()["status"] == "CONFIGURING"

    def test_an_untestable_feature_blocks_readiness(self, client: TestClient) -> None:
        """A model with no defensible neutral value has nothing that can be intervened on."""
        model_id = onboard(client, complete=False)
        client.post(
            "/api/v1/feature-semantics",
            json={
                "model_id": "triage-model",
                "name": "age",
                "data_type": "CONTINUOUS",
                "minimum": 0.0,
                "maximum": 1.0,
                "neutral_strategy": "EXPLICIT",
            },
        )
        readiness = client.get(f"/api/v1/registered-models/{model_id}/readiness").json()
        features = next(c for c in readiness["checks"] if c["name"] == "FEATURE_SEMANTICS")
        assert features["passed"] is False
        assert "none testable" in features["detail"]

    def test_readiness_never_returns_a_scientific_verdict(self, client: TestClient) -> None:
        """Onboarding state and scientific state are different vocabularies."""
        model_id = onboard(client, complete=False)
        body = client.get(f"/api/v1/registered-models/{model_id}/readiness").text
        for verdict in ("SUPPORTED", "CONTRADICTED", "INCONCLUSIVE"):
            assert verdict not in body

    def test_an_unknown_model_is_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/registered-models/MDL-nope/readiness").status_code == 404
