"""Builders for well-formed domain objects.

Tests should say what is different about their case, not restate twenty fields that are the
same every time. Everything here is deterministic, so two tests building "the same" claim
get byte-identical objects.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.core.enums import ExpectedDirection, InterventionType
from backend.core.hashing import sha256_hex
from backend.core.ids import claim_family_id, claim_id, experiment_id
from backend.core.schemas import (
    AgentProvenance,
    Claim,
    ConstraintSpec,
    ExperimentPlan,
    InterventionSpec,
    VersionScope,
)
from backend.core.versions import PROTOCOL_VERSION

T0 = datetime(2026, 1, 1, tzinfo=UTC)
MODEL_ID = "synthetic-triage"
STANDING_EXPLANATION = "Urgency marker was the primary driver."

FIXTURE_FOR_DISTRIBUTION = {
    "baseline_2024.1": "triage_baseline_v1",
    "shifted_2025.2": "triage_shifted_v1",
}


def make_provenance(agent_id: str = "investigator", **overrides: Any) -> AgentProvenance:
    fields: dict[str, Any] = {
        "agent_id": agent_id,
        "agent_version": "1.0.0",
        "role": "INVESTIGATOR",
        "llm_model": "stub",
        "prompt_version": "claim-compiler/1.0.0",
        "temperature": 0.0,
        "produced_at": T0,
    }
    fields.update(overrides)
    return AgentProvenance(**fields)


def make_scope(
    version: str = "1.0.0", distribution: str = "baseline_2024.1", **overrides: Any
) -> VersionScope:
    fields: dict[str, Any] = {
        "model_id": MODEL_ID,
        "model_version": version,
        "distribution_version": distribution,
    }
    fields.update(overrides)
    return VersionScope(**fields)


def make_claim(
    version: str = "1.0.0",
    distribution: str = "baseline_2024.1",
    *,
    investigation_id: str = "INV-test",
    **overrides: Any,
) -> Claim:
    """The demo claim: 'urgency marker was the primary driver', scoped to one version."""
    scope = overrides.pop("scope", make_scope(version, distribution))
    family = claim_family_id(MODEL_ID, "urgency_marker", "is_primary_driver", "priority_score")
    fields: dict[str, Any] = {
        "id": claim_id(family, scope.model_version, scope.distribution_version),
        "claim_family_id": family,
        "investigation_id": investigation_id,
        "scope": scope,
        "source_explanation": STANDING_EXPLANATION,
        "source_explanation_hash": sha256_hex(STANDING_EXPLANATION),
        "source_decision": "HIGH_PRIORITY",
        "subject": "urgency_marker",
        "predicate": "is_primary_driver",
        "object": "priority_score",
        "expected_direction": ExpectedDirection.DECREASE,
        "expected_effect": 0.10,
        "primacy_claim": True,
        "target_variables": ["urgency_marker"],
        "preserved_constraints": ["signal_b"],
        "assumptions": ["neutralizing urgency means setting it to the population midpoint"],
        "testability_score": 0.92,
        "confidence": 0.80,
        "valid_from": T0,
        "provenance": make_provenance(),
    }
    fields.update(overrides)
    return Claim(**fields)


def make_plan(claim: Claim, **overrides: Any) -> ExperimentPlan:
    """A well-formed probe of the claim: neutralize urgency, control on signal_c."""
    repetitions = overrides.pop("repetitions", 24)
    seed = overrides.pop("seed", 20260101)
    fields: dict[str, Any] = {
        "id": experiment_id(claim.id, PROTOCOL_VERSION, seed, repetitions),
        "claim_id": claim.id,
        "investigation_id": claim.investigation_id,
        "scope": claim.scope,
        "intervention": InterventionSpec(
            variable="urgency_marker",
            intervention_type=InterventionType.NEUTRALIZE,
            value=0.5,
        ),
        "control": InterventionSpec(
            variable="signal_c",
            intervention_type=InterventionType.NEUTRALIZE,
            value=0.5,
        ),
        "constraints": ConstraintSpec(preserved_features=["signal_b"], tolerance=1e-9),
        "fixture_set": FIXTURE_FOR_DISTRIBUTION[claim.scope.distribution_version],
        "repetitions": repetitions,
        "seed": seed,
        "expected_direction": ExpectedDirection.DECREASE,
        "min_effect_threshold": 0.10,
        "created_at": T0,
        "provenance": make_provenance("experimenter", role="EXPERIMENTER"),
    }
    fields.update(overrides)
    return ExperimentPlan(**fields)


def make_case(
    version: str = "1.0.0", distribution: str = "baseline_2024.1", **plan_overrides: Any
) -> tuple[Claim, ExperimentPlan]:
    """A matched claim and plan, the pair almost every engine test needs."""
    claim = make_claim(version, distribution)
    return claim, make_plan(claim, **plan_overrides)
