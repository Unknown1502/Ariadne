"""API contract tests.

Two things matter beyond the endpoints returning 200.

First, the console must be able to tell the whole story from these responses - decision,
claim, experiment, evidence, verdict, action - because a UI that has to invent the
connective tissue is a UI that can show something the ledger does not say.

Second, emitting an event must return *before* any verdict exists. If the POST blocked
until the audit finished, the demo's "nobody clicked Analyze" claim would be theatre.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'api.sqlite3').as_posix()}")
    monkeypatch.setenv("DEFAULT_REPETITIONS", "8")
    from backend.config import reset_settings_cache

    reset_settings_cache()
    import backend.api.main as api

    with TestClient(api.app) as test_client:
        yield test_client
    reset_settings_cache()


def deploy(client: TestClient, version: str, **body) -> dict:
    response = client.post(
        "/api/v1/events/model-version-deployed",
        json={"model_version": version, **body},
    )
    assert response.status_code == 200, response.text
    return response.json()


def drain(client: TestClient, expected: int = 1, timeout: float = 30.0) -> None:
    """Wait until the background worker has finished the queued events.

    Polls the API the way a real client would rather than reaching into the bus. The worker
    runs on the app's own event loop, so driving it from a second loop here would be both
    wrong and flaky.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = client.get("/api/v1/investigations").json()["investigations"]
        finished = [
            r for r in rows
            if r["state"] in ("COMPLETE", "REVIEW", "FAILED", "QUARANTINED")
        ]
        if len(finished) >= expected:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"worker did not finish {expected} investigation(s) within {timeout}s"
    )


class TestHealthAndMetadata:
    def test_health(self, client: TestClient) -> None:
        assert client.get("/health").json()["status"] == "ok"

    def test_system_declares_the_synthetic_boundary(self, client: TestClient) -> None:
        body = client.get("/api/v1/system").json()
        assert "no clinical validity" in body["disclaimer"].lower()

    def test_system_reports_the_reasoner_honestly(self, client: TestClient) -> None:
        # The console must never show a Gemini badge over the offline reasoner.
        body = client.get("/api/v1/system").json()
        assert body["reasoner"]["provider"] == "stub"
        assert body["reasoner"]["is_language_model"] is False
        assert "offline" in body["reasoner"]["model"]

    def test_the_model_registry_publishes_its_formulas(self, client: TestClient) -> None:
        # A judge can check the ground truth by hand against these.
        body = client.get("/api/v1/models/synthetic-triage").json()
        assert len(body["versions"]) == 4
        assert all("formula" in v for v in body["versions"])

    def test_an_unknown_model_is_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/models/gpt-9").status_code == 404


class TestEventEmission:
    def test_emitting_returns_an_acknowledgement_not_a_verdict(
        self, client: TestClient
    ) -> None:
        # The demo's core claim: publishing an event is fire-and-forget. The response
        # carries an acknowledgement and nothing resembling a result, because the audit has
        # not run yet and the API is not the thing that decides it.
        body = deploy(client, "1.0.0")
        assert body["accepted"] is True
        assert body["idempotency_key"]
        assert "verdict" not in body
        assert "status" not in body
        assert "queued" in body["note"]

    def test_the_worker_completes_the_audit_with_no_further_client_action(
        self, client: TestClient
    ) -> None:
        # After the POST, the client only ever reads. Nothing triggers the audit.
        deploy(client, "1.0.0")
        drain(client)
        investigations = client.get("/api/v1/investigations").json()["investigations"]
        assert len(investigations) == 1
        assert investigations[0]["verdict"]["status"] == "CONTRADICTED"

    def test_a_duplicate_event_produces_one_investigation(self, client: TestClient) -> None:
        deploy(client, "1.0.0", duplicate=True)
        drain(client)
        assert len(client.get("/api/v1/investigations").json()["investigations"]) == 1
        runtime = client.get("/api/v1/runtime").json()
        assert runtime["worker"]["duplicates_skipped"] >= 1

    def test_an_invalid_version_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/events/model-version-deployed", json={"model_version": "not-semver"}
        )
        assert response.status_code in (400, 422)


class TestInvestigationNarrative:
    def test_the_full_story_is_available_in_one_call(self, client: TestClient) -> None:
        deploy(client, "1.0.0")
        drain(client)
        investigation_id = client.get("/api/v1/investigations").json()["investigations"][0]["id"]
        body = client.get(f"/api/v1/investigations/{investigation_id}").json()

        # decision -> explanation -> claim -> experiment -> evidence -> verdict -> action
        assert body["decision"]["explanation"]
        assert body["claim"]["subject"] == "urgency_marker"
        assert body["experiment"]["intervention"]["variable"] == "urgency_marker"
        assert body["evidence"]["baseline"]["n"] == 8
        assert body["verdict"]["status"] == "CONTRADICTED"
        assert body["action"]["action"]
        assert body["debt"]["total"] >= 0

    def test_the_verdict_links_to_its_evidence(self, client: TestClient) -> None:
        deploy(client, "1.0.0")
        drain(client)
        investigation_id = client.get("/api/v1/investigations").json()["investigations"][0]["id"]
        body = client.get(f"/api/v1/investigations/{investigation_id}").json()
        assert body["evidence"]["id"] in body["verdict"]["evidence_ids"]

    def test_the_claim_uses_its_wire_alias(self, client: TestClient) -> None:
        deploy(client, "1.0.0")
        drain(client)
        investigation_id = client.get("/api/v1/investigations").json()["investigations"][0]["id"]
        claim = client.get(f"/api/v1/investigations/{investigation_id}").json()["claim"]
        assert claim["object"] == "priority_score"

    def test_an_unknown_investigation_is_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/investigations/INV-nope").status_code == 404


class TestLineageAndDebt:
    def test_lineage_reports_every_version(self, client: TestClient) -> None:
        for index, version in enumerate(("1.0.0", "2.0.0", "3.0.0"), start=1):
            deploy(client, version)
            drain(client, expected=index)

        family = client.get("/api/v1/claim-families").json()["families"][0]
        body = client.get(f"/api/v1/lineage/{family['claim_family_id']}").json()
        assert body["statuses_by_version"] == {
            "1.0.0": "CONTRADICTED",
            "2.0.0": "SUPPORTED",
            "3.0.0": "INCONCLUSIVE",
        }
        assert body["chain_intact"] is True

    def test_lineage_can_be_read_at_a_past_moment(self, client: TestClient) -> None:
        deploy(client, "1.0.0")
        drain(client)
        family = client.get("/api/v1/claim-families").json()["families"][0][
            "claim_family_id"
        ]
        body = client.get(f"/api/v1/lineage/{family}?at=2020-01-01T00:00:00Z").json()
        assert body["current"] is None  # nothing had been measured yet

    def test_an_invalid_timestamp_is_rejected(self, client: TestClient) -> None:
        deploy(client, "1.0.0")
        drain(client)
        family = client.get("/api/v1/claim-families").json()["families"][0][
            "claim_family_id"
        ]
        assert client.get(f"/api/v1/lineage/{family}?at=yesterday").status_code == 400

    def test_debt_exposes_its_breakdown(self, client: TestClient) -> None:
        deploy(client, "1.0.0")
        drain(client)
        body = client.get("/api/v1/debt/synthetic-triage").json()
        assert body["current"]["total"] > 0
        assert len(body["current"]["components"]) == 5
        for component in body["current"]["components"]:
            assert component["points"] == pytest.approx(
                component["ratio"] * component["weight"]
            )
        assert "Policy version" in body["rendered"]

    def test_debt_history_accumulates(self, client: TestClient) -> None:
        deploy(client, "1.0.0")
        drain(client, expected=1)
        deploy(client, "2.0.0")
        drain(client, expected=2)
        assert len(client.get("/api/v1/debt/synthetic-triage").json()["history"]) == 2

    def test_unknown_lineage_is_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/lineage/FAM-nope").status_code == 404


class TestFleetAndRuntime:
    def test_the_fleet_lists_exactly_four_roles(self, client: TestClient) -> None:
        agents = client.get("/api/v1/fleet").json()["agents"]
        assert len(agents) == 4
        assert {a["role"] for a in agents} == {
            "INVESTIGATOR", "EXPERIMENTER", "VERIFIER", "GOVERNOR"
        }

    def test_the_verifier_is_shown_as_llm_free(self, client: TestClient) -> None:
        agents = client.get("/api/v1/fleet").json()["agents"]
        verifier = next(a for a in agents if a["role"] == "VERIFIER")
        assert verifier["uses_llm"] is False
        assert verifier["tools"] == []

    def test_runtime_proves_the_machinery_ran(self, client: TestClient) -> None:
        deploy(client, "1.0.0")
        drain(client)
        body = client.get("/api/v1/runtime").json()
        assert body["bus"]["published"] >= 1
        assert body["worker"]["events_processed"] >= 1
        assert body["ledger"]["evidence"] >= 1
        assert body["checkpoints"]["runs"] > 0

    def test_runtime_reports_ledger_integrity(self, client: TestClient) -> None:
        deploy(client, "1.0.0")
        drain(client)
        integrity = client.get("/api/v1/runtime").json()["integrity"]
        assert integrity["lineage_chain_broken_rows"] == []
        assert integrity["verdict_rows_broken"] == []


class TestApprovalGate:
    def _reach_review(self, client: TestClient) -> dict:
        deploy(client, "1.0.0")
        drain(client, expected=1)
        deploy(client, "4.0.0")
        drain(client, expected=2)
        pending = client.get("/api/v1/approvals").json()["pending"]
        assert pending, "two contradictions should require human review"
        return pending[0]

    def test_a_high_impact_action_waits_for_a_human(self, client: TestClient) -> None:
        request = self._reach_review(client)
        assert request["status"] == "PENDING"
        assert request["action"] in ("REQUIRE_HUMAN_REVIEW", "PAUSE_AFFECTED_WORKFLOW")

    def test_a_decision_is_recorded_with_its_decider(self, client: TestClient) -> None:
        request = self._reach_review(client)
        response = client.post(
            f"/api/v1/approvals/{request['id']}/decide",
            json={"approve": True, "decided_by": "nurse-supervisor", "note": "acknowledged"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "APPROVED"
        assert body["decided_by"] == "nurse-supervisor"
        assert body["decided_at"]

    def test_deciding_twice_is_refused(self, client: TestClient) -> None:
        request = self._reach_review(client)
        client.post(
            f"/api/v1/approvals/{request['id']}/decide",
            json={"approve": True, "decided_by": "supervisor"},
        )
        second = client.post(
            f"/api/v1/approvals/{request['id']}/decide",
            json={"approve": False, "decided_by": "someone-else"},
        )
        assert second.status_code == 409

    def test_an_unknown_approval_is_404(self, client: TestClient) -> None:
        assert (
            client.post(
                "/api/v1/approvals/APR-nope/decide",
                json={"approve": True, "decided_by": "x"},
            ).status_code
            == 404
        )


class TestApiIsNotTheSourceOfTruth:
    def test_there_is_no_endpoint_that_writes_a_verdict(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        writable = [
            path
            for path, methods in paths.items()
            if "post" in methods or "put" in methods or "patch" in methods
        ]
        # Only event emission and the human approval gate may write.
        assert all(
            path.startswith("/api/v1/events/") or "/approvals/" in path
            for path in writable
        ), writable

    def test_no_endpoint_accepts_a_verdict_value(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        text = str(schema)
        for body_schema in schema["components"].get("schemas", {}).values():
            properties = body_schema.get("properties", {})
            assert "status" not in properties or "Approval" in str(body_schema.get("title", ""))
        assert "SUPPORTED" not in text
