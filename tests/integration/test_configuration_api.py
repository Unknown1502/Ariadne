"""Connections, feature semantics, and explanation sources, through the real API.

These three resources exist because an organisation cannot use Ariadne without them, and
they were the last part of the system that lived as constants in the laboratory. The tests
here are organised around the three rules that stop the configuration layer from lying:

  status is earned, never asserted
  validation is scientific, not cosmetic
  a configuration failure is never a scientific verdict

The third is the one worth stating twice. An unreachable endpoint must produce a FAILED
connection and never a CONTRADICTED claim; an undefined neutral value must produce a
NOT_TESTABLE feature and never an INCONCLUSIVE verdict that looks like a scientific finding.
Those are different vocabularies on purpose, and a test that lets them blur would let the
product start manufacturing science out of infrastructure problems.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A real app against a throwaway runtime directory.

    Not a mock: the routes, the store, and the prober are the production ones. Only the
    directory is disposable.
    """
    from backend.config import reset_settings_cache

    previous = os.environ.get("VAR_DIR")
    os.environ["VAR_DIR"] = tempfile.mkdtemp()
    # Two things had to be right here, and getting either wrong makes the whole file
    # meaningless rather than merely failing. `runtime_dir` derives from `var_dir`, so
    # setting RUNTIME_DIR (the obvious guess) isolates nothing; and settings are lru_cached,
    # so without a reset every test shares the first directory. The symptom was a test
    # counting six live connections where it had created one - which is exactly the kind of
    # accumulated-state result that looks like a passing suite until it does not.
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


LAB = {
    "kind": "MODEL_ENDPOINT",
    "name": "Synthetic laboratory",
    "transport": "IN_PROCESS",
    "model_version": "2.0.0",
}


class TestStatusIsEarnedNeverAsserted:
    def test_a_new_connection_is_not_configured(self, client: TestClient) -> None:
        """Typing a URL into a form must not make anything look connected."""
        created = client.post("/api/v1/connections", json=LAB)
        assert created.status_code == 201
        assert created.json()["status"] == "NOT_CONFIGURED"
        assert created.json()["last_success_at"] is None

    def test_the_create_body_has_no_way_to_claim_health(self, client: TestClient) -> None:
        """Even asking for OK explicitly must not produce it."""
        response = client.post("/api/v1/connections", json={**LAB, "status": "OK"})
        # Either the field is rejected outright or it is ignored; both are acceptable,
        # a connection that comes back OK is not.
        if response.status_code == 201:
            assert response.json()["status"] == "NOT_CONFIGURED"
        else:
            assert response.status_code == 422

    def test_a_real_probe_moves_it_to_ok(self, client: TestClient) -> None:
        connection_id = client.post("/api/v1/connections", json=LAB).json()["id"]
        probe = client.post(f"/api/v1/connections/{connection_id}/test")

        assert probe.status_code == 200
        assert probe.json()["ok"] is True
        assert client.get(f"/api/v1/connections/{connection_id}").json()["status"] == "OK"

    def test_the_probe_reports_what_it_actually_checked(self, client: TestClient) -> None:
        """A green tick nobody can audit is a green tick nobody should believe."""
        connection_id = client.post("/api/v1/connections", json=LAB).json()["id"]
        checks = client.post(f"/api/v1/connections/{connection_id}/test").json()["checks"]

        names = {check["name"] for check in checks}
        assert {"transport", "model version", "response schema"} <= names
        assert all(check["detail"] for check in checks), "every check must say what it saw"

    def test_an_unreachable_endpoint_fails_rather_than_hanging_on_ok(
        self, client: TestClient
    ) -> None:
        created = client.post(
            "/api/v1/connections",
            json={
                "kind": "MODEL_ENDPOINT",
                "name": "Nowhere",
                "transport": "HTTP",
                "endpoint": "https://not-a-real-host.invalid/predict",
                "timeout_seconds": 3.0,
            },
        ).json()
        probe = client.post(f"/api/v1/connections/{created['id']}/test").json()

        assert probe["ok"] is False
        stored = client.get(f"/api/v1/connections/{created['id']}").json()
        assert stored["status"] == "FAILED"
        assert stored["last_error"]
        assert stored["last_failure_at"] is not None

    def test_editing_configuration_invalidates_the_previous_result(
        self, client: TestClient
    ) -> None:
        """Keeping a green tick earned by the *old* endpoint is the stale-truth problem
        this entire project argues against, applied to infrastructure."""
        connection_id = client.post("/api/v1/connections", json=LAB).json()["id"]
        client.post(f"/api/v1/connections/{connection_id}/test")
        assert client.get(f"/api/v1/connections/{connection_id}").json()["status"] == "OK"

        patched = client.patch(
            f"/api/v1/connections/{connection_id}", json={"model_version": "4.0.0"}
        ).json()
        assert patched["status"] == "NOT_CONFIGURED"
        assert patched["configuration_version"] == 2

    def test_disabling_a_connection_stops_it_being_live(self, client: TestClient) -> None:
        connection_id = client.post("/api/v1/connections", json=LAB).json()["id"]
        client.post(f"/api/v1/connections/{connection_id}/test")

        disabled = client.post(f"/api/v1/connections/{connection_id}/disable").json()
        assert disabled["enabled"] is False
        assert disabled["status"] == "DISABLED"
        assert client.get("/api/v1/connections").json()["live"] == 0


class TestCredentialsAreNeverStored:
    @pytest.mark.parametrize(
        "secret",
        [
            "sk-abcdef0123456789abcdef",
            "AIzaSyDUMMYKEYVALUEFORTESTING1234567",
            "-----BEGIN PRIVATE KEY-----",
            "x" * 120,
        ],
    )
    def test_a_credential_that_looks_like_a_secret_is_refused(
        self, client: TestClient, secret: str
    ) -> None:
        """A leaked configuration document must leak nothing that grants access."""
        response = client.post(
            "/api/v1/connections",
            json={
                "kind": "MODEL_ENDPOINT",
                "name": "leaky",
                "transport": "HTTP",
                "endpoint": "https://example.invalid",
                "credential_ref": secret,
            },
        )
        assert response.status_code == 422

    def test_a_reference_to_a_secret_is_accepted(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/connections",
            json={
                "kind": "MODEL_ENDPOINT",
                "name": "referenced",
                "transport": "HTTP",
                "endpoint": "https://example.invalid",
                "credential_ref": "projects/p/secrets/model-key/versions/latest",
            },
        )
        assert response.status_code == 201


class TestFeatureValidationIsScientific:
    BASE = {
        "model_id": "synthetic-triage",
        "name": "urgency_marker",
        "data_type": "CONTINUOUS",
        "minimum": 0.0,
        "maximum": 1.0,
    }

    def test_a_defensible_feature_becomes_testable(self, client: TestClient) -> None:
        created = client.post(
            "/api/v1/feature-semantics",
            json={**self.BASE, "neutral_strategy": "EXPLICIT", "neutral_value": 0.5},
        ).json()
        assert created["validated"] is True
        assert created["validation_errors"] == []

        detail = client.get(f"/api/v1/feature-semantics/{created['id']}").json()
        assert detail["resolved_neutral"] == 0.5

    def test_a_neutral_value_outside_the_range_is_refused(self, client: TestClient) -> None:
        """The intervention would produce an input the model should never see."""
        created = client.post(
            "/api/v1/feature-semantics",
            json={**self.BASE, "neutral_strategy": "EXPLICIT", "neutral_value": 1.7},
        ).json()
        assert created["validated"] is False
        assert any("outside the declared range" in e for e in created["validation_errors"])

    def test_a_missing_neutral_value_is_refused(self, client: TestClient) -> None:
        """There is no default. A neutral value nobody chose is a counterfactual nobody
        can defend, and defaulting it would silently decide the science."""
        created = client.post(
            "/api/v1/feature-semantics",
            json={**self.BASE, "neutral_strategy": "EXPLICIT"},
        ).json()
        assert created["validated"] is False
        assert any("requires a neutral value" in e for e in created["validation_errors"])

    def test_every_problem_is_reported_not_just_the_first(self, client: TestClient) -> None:
        """An integrator fixing a definition should see the whole list."""
        created = client.post(
            "/api/v1/feature-semantics",
            json={
                "model_id": "m",
                "name": "employment",
                "data_type": "CATEGORICAL",
                "neutral_strategy": "REFERENCE_CATEGORY",
                "neutral_category": "unknown",
            },
        ).json()
        assert created["validated"] is False
        assert any("allowed values" in e for e in created["validation_errors"])

    def test_a_categorical_feature_cannot_use_a_numeric_strategy(
        self, client: TestClient
    ) -> None:
        """A median of unordered categories is not a value the model can be given."""
        response = client.post(
            "/api/v1/feature-semantics",
            json={
                "model_id": "m",
                "name": "employment",
                "data_type": "CATEGORICAL",
                "allowed_values": ["employed", "unknown"],
                "neutral_strategy": "POPULATION_MEDIAN",
            },
        )
        assert response.status_code == 422

    def test_revalidation_explains_why_a_feature_is_not_testable(
        self, client: TestClient
    ) -> None:
        """The message must say Ariadne will not manufacture a verdict - not imply one."""
        created = client.post(
            "/api/v1/feature-semantics",
            json={**self.BASE, "neutral_strategy": "EXPLICIT", "neutral_value": 9.0},
        ).json()
        result = client.post(f"/api/v1/feature-semantics/{created['id']}/validate").json()

        assert result["testable"] is False
        assert result["resolved_neutral"] is None
        assert "will not manufacture a verdict" in result["reason"]
        for verdict in ("SUPPORTED", "CONTRADICTED", "INCONCLUSIVE"):
            assert verdict not in result["reason"], (
                "a configuration problem must not be phrased as a scientific verdict"
            )

    def test_revising_a_neutral_value_takes_a_new_version(self, client: TestClient) -> None:
        """Changing what neutral means is a scientific act, not an edit."""
        created = client.post(
            "/api/v1/feature-semantics",
            json={**self.BASE, "neutral_strategy": "EXPLICIT", "neutral_value": 0.5},
        ).json()
        updated = client.patch(
            f"/api/v1/feature-semantics/{created['id']}",
            json={**self.BASE, "neutral_strategy": "EXPLICIT", "neutral_value": 0.4},
        ).json()

        assert updated["configuration_version"] == created["configuration_version"] + 1
        assert updated["neutral_value"] == 0.4


class TestExplanationIngestion:
    SOURCE = {
        "model_id": "synthetic-triage",
        "name": "Triage model response",
        "source_type": "MODEL_RESPONSE",
    }

    def test_an_explanation_is_stored_verbatim(self, client: TestClient) -> None:
        """The claim compiled from an explanation is an interpretation. An interpretation
        whose source was discarded cannot be audited, or re-compiled when the compiler
        improves."""
        source_id = client.post("/api/v1/explanation-sources", json=self.SOURCE).json()["id"]
        text = "Nothing else came close; the urgency score decided this one."

        accepted = client.post(
            f"/api/v1/explanation-sources/{source_id}/ingest",
            json={
                "model_version": "2.0.0",
                "distribution_version": "baseline_2024.1",
                "decision": "HIGH_PRIORITY",
                "explanation": text,
            },
        )
        assert accepted.status_code == 202
        assert accepted.json()["event_id"].startswith("EVT-")

        stored = client.get("/api/v1/explanations").json()["explanations"]
        assert len(stored) == 1
        assert stored[0]["explanation"] == text, "stored verbatim, not normalised"
        assert stored[0]["model_version"] == "2.0.0"

    def test_ingestion_publishes_onto_the_same_bus_as_every_other_event(
        self, client: TestClient
    ) -> None:
        """Ingestion must not become a second pipeline running beside the real one."""
        before = client.get("/api/v1/runtime").json()["bus"]["published"]
        source_id = client.post("/api/v1/explanation-sources", json=self.SOURCE).json()["id"]
        client.post(
            f"/api/v1/explanation-sources/{source_id}/ingest",
            json={
                "model_version": "2.0.0",
                "distribution_version": "baseline_2024.1",
                "decision": "HIGH_PRIORITY",
                    "explanation": "urgency drove it",
            },
        )
        assert client.get("/api/v1/runtime").json()["bus"]["published"] == before + 1

    def test_the_source_records_what_it_has_received(self, client: TestClient) -> None:
        source_id = client.post("/api/v1/explanation-sources", json=self.SOURCE).json()["id"]
        for _ in range(3):
            client.post(
                f"/api/v1/explanation-sources/{source_id}/ingest",
                json={
                    "model_version": "2.0.0",
                    "distribution_version": "baseline_2024.1",
                    "decision": "HIGH_PRIORITY",
                    "explanation": "urgency drove it",
                },
            )
        source = client.get(f"/api/v1/explanation-sources/{source_id}").json()
        assert source["received_count"] == 3
        assert source["last_received_at"] is not None

    def test_an_unknown_source_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/explanation-sources/SRC-does-not-exist/ingest",
            json={
                "model_version": "2.0.0",
                "distribution_version": "baseline_2024.1",
                "decision": "HIGH_PRIORITY",
                "explanation": "x",
            },
        )
        assert response.status_code == 404

    def test_an_empty_explanation_is_refused(self, client: TestClient) -> None:
        source_id = client.post("/api/v1/explanation-sources", json=self.SOURCE).json()["id"]
        response = client.post(
            f"/api/v1/explanation-sources/{source_id}/ingest",
            json={
                "model_version": "2.0.0",
                "distribution_version": "baseline_2024.1",
                "decision": "HIGH_PRIORITY",
                "explanation": "",
            },
        )
        assert response.status_code == 422

    def test_an_endpoint_source_needs_an_endpoint(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/explanation-sources",
            json={
                "model_id": "m",
                "name": "explainer",
                "source_type": "EXPLANATION_ENDPOINT",
            },
        )
        assert response.status_code == 422


class TestConfigurationNeverProducesScience:
    """The boundary that keeps infrastructure failure out of the verdict vocabulary."""

    def test_no_configuration_route_can_return_a_verdict(self, client: TestClient) -> None:
        source_id = client.post(
            "/api/v1/explanation-sources",
            json={"model_id": "m", "name": "s", "source_type": "MODEL_RESPONSE"},
        ).json()["id"]
        connection_id = client.post("/api/v1/connections", json=LAB).json()["id"]

        payloads = [
            client.get("/api/v1/connections").text,
            client.post(f"/api/v1/connections/{connection_id}/test").text,
            client.get("/api/v1/feature-semantics").text,
            client.get(f"/api/v1/explanation-sources/{source_id}").text,
        ]
        for body in payloads:
            for verdict in ("SUPPORTED", "CONTRADICTED", "INCONCLUSIVE"):
                assert verdict not in body, (
                    "a configuration endpoint returned a scientific verdict; infrastructure "
                    "state and verdicts must stay in different vocabularies"
                )

    def test_deleting_configuration_does_not_touch_evidence(self, client: TestClient) -> None:
        """Configuration is mutable. Evidence is not, and lives somewhere else entirely."""
        before = client.get("/api/v1/runtime").json()["ledger"]
        connection_id = client.post("/api/v1/connections", json=LAB).json()["id"]
        assert client.delete(f"/api/v1/connections/{connection_id}").status_code == 204
        assert client.get("/api/v1/runtime").json()["ledger"] == before

    def test_a_deleted_connection_is_gone(self, client: TestClient) -> None:
        connection_id = client.post("/api/v1/connections", json=LAB).json()["id"]
        client.delete(f"/api/v1/connections/{connection_id}")
        assert client.get(f"/api/v1/connections/{connection_id}").status_code == 404
        assert client.delete(f"/api/v1/connections/{connection_id}").status_code == 404
