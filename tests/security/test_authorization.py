"""Backend authorization, proved by being refused.

Frontend permission checks are UX. This file is about the security boundary, and the only
way to establish a boundary exists is to stand on the wrong side of it and be turned away.
So every allow test here has a matching deny test against the same endpoint.

Two properties matter more than the individual grants:

**Enforcement is honestly reported.** The API runs OPEN when no keys are configured, which is
right for `pytest` and for a local demo. What would be wrong is running OPEN while looking
protected, so `/api/v1/system` publishes the mode and a test below asserts it changes when
enforcement does.

**No human role can write a verdict.** There is no such permission in the table. A verdict
comes from measurements through the verifier, and a role that could override it would dissolve
the property the rest of the system rests on - so the absence is asserted rather than assumed.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from backend.api.authz import GRANTS, Permission, Role, auth_mode

KEYS = (
    "admin-key:alice:MODEL_ADMIN,"
    "review-key:bob:REVIEWER,"
    "operator-key:registry:OPERATOR,"
    "audit-key:auditor:READ_ONLY"
)

LAB = {
    "kind": "MODEL_ENDPOINT",
    "name": "Synthetic laboratory",
    "transport": "IN_PROCESS",
    "model_version": "2.0.0",
}


@pytest.fixture
def enforced() -> Iterator[TestClient]:
    """A client against an API with real keys configured."""
    from backend.config import reset_settings_cache

    previous_keys = os.environ.get("ARIADNE_API_KEYS")
    previous_var = os.environ.get("VAR_DIR")
    os.environ["ARIADNE_API_KEYS"] = KEYS
    os.environ["VAR_DIR"] = tempfile.mkdtemp()
    reset_settings_cache()
    try:
        from backend.api.main import app

        with TestClient(app) as client:
            yield client
    finally:
        for name, value in (("ARIADNE_API_KEYS", previous_keys), ("VAR_DIR", previous_var)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        reset_settings_cache()


def headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


class TestTheModeIsReportedHonestly:
    def test_it_is_open_when_no_keys_are_configured(self) -> None:
        previous = os.environ.pop("ARIADNE_API_KEYS", None)
        try:
            assert auth_mode() == "OPEN"
        finally:
            if previous is not None:
                os.environ["ARIADNE_API_KEYS"] = previous

    def test_it_is_enforced_when_keys_are(self, enforced: TestClient) -> None:
        assert auth_mode() == "ENFORCED"

    def test_the_system_endpoint_publishes_it(self, enforced: TestClient) -> None:
        """A reviewer who cannot read the configuration can still answer 'is this
        protected?'. That is the whole point of the honesty endpoint."""
        response = enforced.get("/api/v1/system", headers=headers("audit-key"))
        assert response.json()["authorization"] == "ENFORCED"

    def test_a_malformed_key_entry_is_skipped_rather_than_crashing(self) -> None:
        """A typo in one credential must not take the whole API down."""
        previous = os.environ.get("ARIADNE_API_KEYS")
        os.environ["ARIADNE_API_KEYS"] = "good:alice:MODEL_ADMIN,nonsense,bad:bob:WIZARD"
        try:
            from backend.api.authz import _configured_keys

            table = _configured_keys()
            assert set(table) == {"good"}, "only the well-formed entry should grant anything"
        finally:
            if previous is None:
                os.environ.pop("ARIADNE_API_KEYS", None)
            else:
                os.environ["ARIADNE_API_KEYS"] = previous


class TestCredentialsAreRequired:
    def test_a_missing_key_is_refused(self, enforced: TestClient) -> None:
        assert enforced.get("/api/v1/connections").status_code == 401

    def test_an_unknown_key_is_refused(self, enforced: TestClient) -> None:
        assert enforced.get("/api/v1/connections", headers=headers("nope")).status_code == 401

    def test_a_valid_key_is_accepted(self, enforced: TestClient) -> None:
        assert enforced.get("/api/v1/connections", headers=headers("audit-key")).status_code == 200


class TestReadOnlyCannotChangeAnything:
    """The auditor role. Sees everything, changes nothing."""

    def test_it_can_read(self, enforced: TestClient) -> None:
        response = enforced.get("/api/v1/investigations", headers=headers("audit-key"))
        assert response.status_code == 200

    def test_it_cannot_configure(self, enforced: TestClient) -> None:
        response = enforced.post("/api/v1/connections", json=LAB, headers=headers("audit-key"))
        assert response.status_code == 403

    def test_it_cannot_emit_events(self, enforced: TestClient) -> None:
        response = enforced.post(
            "/api/v1/events/model-version-deployed",
            json={
                "model_id": "synthetic-triage",
                "model_version": "2.0.0",
                "distribution_version": "baseline_2024.1",
            },
            headers=headers("audit-key"),
        )
        assert response.status_code == 403

    def test_the_refusal_says_which_role_would_be_needed(self, enforced: TestClient) -> None:
        """An operator who cannot tell why they were refused assumes the endpoint is broken."""
        detail = enforced.post(
            "/api/v1/connections", json=LAB, headers=headers("audit-key")
        ).json()["detail"]
        assert "READ_ONLY" in detail
        assert "CONFIGURE" in detail
        assert "MODEL_ADMIN" in detail


class TestRolesAreGenuinelyDifferent:
    """Four roles that all permitted the same things would be decoration."""

    def test_an_admin_configures_but_does_not_decide_approvals(
        self, enforced: TestClient
    ) -> None:
        assert enforced.post(
            "/api/v1/connections", json=LAB, headers=headers("admin-key")
        ).status_code == 201
        refused = enforced.post(
            "/api/v1/approvals/APR-anything/decide",
            json={"approve": True, "decided_by": "alice"},
            headers=headers("admin-key"),
        )
        assert refused.status_code == 403, (
            "configuring the system and approving a governance action are different "
            "authorities; one person holding both is a policy choice, not a default"
        )

    def test_a_reviewer_decides_approvals_but_does_not_configure(
        self, enforced: TestClient
    ) -> None:
        # 404 rather than 403: permitted, and the approval simply does not exist.
        decided = enforced.post(
            "/api/v1/approvals/APR-does-not-exist/decide",
            json={"approve": True, "decided_by": "bob"},
            headers=headers("review-key"),
        )
        assert decided.status_code == 404
        assert enforced.post(
            "/api/v1/connections", json=LAB, headers=headers("review-key")
        ).status_code == 403

    def test_an_operator_emits_events_but_does_not_configure(
        self, enforced: TestClient
    ) -> None:
        """The role a model registry would hold: it starts work and configures nothing."""
        assert enforced.post(
            "/api/v1/events/model-version-deployed",
            json={
                "model_id": "synthetic-triage",
                "model_version": "2.0.0",
                "distribution_version": "baseline_2024.1",
            },
            headers=headers("operator-key"),
        ).status_code == 200
        assert enforced.post(
            "/api/v1/connections", json=LAB, headers=headers("operator-key")
        ).status_code == 403


class TestNoRoleCanWriteAVerdict:
    def test_the_permission_does_not_exist(self) -> None:
        """Asserted rather than assumed, because its absence is the load-bearing part."""
        names = {permission.value for permission in Permission}
        for forbidden in ("VERDICT", "WRITE_VERDICT", "OVERRIDE"):
            assert not any(forbidden in name for name in names), (
                f"a {forbidden} permission exists; a human able to write a verdict would "
                "dissolve the property the rest of the system rests on"
            )

    def test_every_role_is_covered_by_the_grant_table(self) -> None:
        """A role with no entry would raise a KeyError at request time instead of denying."""
        assert set(GRANTS) == set(Role)

    def test_read_is_granted_to_everyone(self) -> None:
        """Ariadne's value is auditability. No role may be blind to the evidence."""
        for role, grants in GRANTS.items():
            assert Permission.READ in grants, f"{role} cannot read the evidence"
