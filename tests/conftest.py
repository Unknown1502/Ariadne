"""Shared test fixtures.

Two rules hold across the whole suite:

  1. No test touches the network, and no test needs a Google Cloud account.
  2. No test depends on wall-clock time or unseeded randomness. Every fixture below is
     deterministic, so a failure means a real regression rather than a flake.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.core.agent_contracts import AgentManifest, ToolPermission
from backend.core.clock import ManualClock
from backend.core.enums import AgentRole, ExpectedDirection, InterventionType, RiskLevel
from backend.core.hashing import sha256_hex
from backend.core.ids import claim_family_id, claim_id
from backend.core.schemas import (
    AgentProvenance,
    Claim,
    ConstraintSpec,
    ExperimentPlan,
    InterventionSpec,
    VersionScope,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
MODEL_ID = "synthetic-triage"
DISTRIBUTION = "baseline_2024.1"


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def scope() -> VersionScope:
    return VersionScope(
        model_id=MODEL_ID, model_version="1.0.0", distribution_version=DISTRIBUTION
    )


@pytest.fixture
def provenance() -> AgentProvenance:
    return AgentProvenance(
        agent_id="investigator",
        agent_version="1.0.0",
        role="INVESTIGATOR",
        llm_model="stub",
        prompt_version="claim-compiler/1.0.0",
        temperature=0.0,
        output_hash=sha256_hex({"stub": True}),
        produced_at=T0,
    )


@pytest.fixture
def family_id() -> str:
    return claim_family_id(MODEL_ID, "urgency_marker", "is_primary_driver", "priority_score")


@pytest.fixture
def claim(scope: VersionScope, provenance: AgentProvenance, family_id: str) -> Claim:
    """The demo's canonical claim: 'urgency marker was the primary driver'."""
    explanation = "Urgency marker was the primary driver."
    return Claim(
        id=claim_id(family_id, scope.model_version, scope.distribution_version),
        claim_family_id=family_id,
        investigation_id="INV-test",
        scope=scope,
        source_explanation=explanation,
        source_explanation_hash=sha256_hex(explanation),
        source_decision="HIGH_PRIORITY",
        subject="urgency_marker",
        predicate="is_primary_driver",
        object="priority_score",
        expected_direction=ExpectedDirection.DECREASE,
        expected_effect=0.10,
        primacy_claim=True,
        target_variables=["urgency_marker"],
        preserved_constraints=["signal_b", "signal_c"],
        assumptions=["neutralizing urgency means setting it to the population midpoint"],
        testability_score=0.92,
        confidence=0.80,
        valid_from=T0,
        provenance=provenance,
    )


@pytest.fixture
def plan(claim: Claim, provenance: AgentProvenance) -> ExperimentPlan:
    from backend.core.ids import experiment_id
    from backend.core.versions import PROTOCOL_VERSION

    return ExperimentPlan(
        id=experiment_id(claim.id, PROTOCOL_VERSION, 20260101, 12),
        claim_id=claim.id,
        investigation_id=claim.investigation_id,
        scope=claim.scope,
        intervention=InterventionSpec(
            variable="urgency_marker",
            intervention_type=InterventionType.NEUTRALIZE,
            value=0.5,
        ),
        control=InterventionSpec(
            variable="signal_c",
            intervention_type=InterventionType.NEUTRALIZE,
            value=0.5,
        ),
        constraints=ConstraintSpec(
            preserved_features=["signal_b", "signal_c"],
            tolerance=1e-9,
            feature_bounds={
                "urgency_marker": (0.0, 1.0),
                "signal_c": (0.0, 1.0),
                "signal_b": (0.0, 1.0),
            },
        ),
        fixture_set="triage_baseline_v1",
        repetitions=12,
        seed=20260101,
        expected_direction=ExpectedDirection.DECREASE,
        created_at=T0,
        provenance=provenance.model_copy(update={"agent_id": "experimenter"}),
    )


@pytest.fixture
def investigator_manifest() -> AgentManifest:
    return AgentManifest(
        agent_id="investigator",
        version="1.0.0",
        role=AgentRole.INVESTIGATOR,
        owner="ariadne-team",
        capabilities=["compile_claim"],
        input_schema="ExplanationReceivedPayload",
        output_schema="Claim",
        read_scopes=["lineage.read", "policy.read"],
        write_scopes=["claim"],
        allowed_tools=[ToolPermission(tool="lineage.get_family", max_risk=RiskLevel.READ_ONLY)],
        max_risk_level=RiskLevel.LOW,
        prompt_version="claim-compiler/1.0.0",
    )


@pytest.fixture
def tmp_var_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate on-disk state per test so runs never share a ledger."""
    var = tmp_path / "var"
    var.mkdir()
    monkeypatch.setenv("VAR_DIR", str(var))
    monkeypatch.setenv("DATABASE_URL", "")
    from backend.config import reset_settings_cache

    reset_settings_cache()
    yield var
    reset_settings_cache()
