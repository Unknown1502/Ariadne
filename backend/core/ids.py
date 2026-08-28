"""Deterministic identifiers.

Ariadne derives identifiers from content rather than from randomness. Two consequences
matter operationally:

  1. Re-running the same investigation produces the same IDs, so an at-least-once event
     delivery cannot create a second copy of the same claim or experiment.
  2. An ID is itself a provenance statement: it is a function of the inputs that defined
     the object.

Only genuinely external things (an inbound event from another system) carry random IDs.
"""

from __future__ import annotations

import uuid
from typing import Any

from backend.core.hashing import short_hash

CLAIM_FAMILY_PREFIX = "FAM"
CLAIM_PREFIX = "CLM"
INVESTIGATION_PREFIX = "INV"
EXPERIMENT_PREFIX = "EXP"
RUN_PREFIX = "RUN"
EVIDENCE_PREFIX = "EVD"
VERDICT_PREFIX = "VDT"
LINEAGE_PREFIX = "LIN"
DEBT_PREFIX = "DBT"
DECISION_PREFIX = "GOV"
EVENT_PREFIX = "EVT"
APPROVAL_PREFIX = "APR"


def derive_id(prefix: str, *parts: Any) -> str:
    """Content-addressed identifier: same inputs always yield the same ID."""
    return f"{prefix}-{short_hash(list(parts))}"


def random_id(prefix: str) -> str:
    """Random identifier, for events genuinely originating outside Ariadne."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def claim_family_id(model_id: str, subject: str, predicate: str, object_: str) -> str:
    """Identify a claim *family*: the same assertion across every model version.

    Deliberately excludes model_version and distribution_version. That exclusion is what
    lets lineage follow one claim from v1 to v4 instead of treating each version's claim
    as an unrelated record.
    """
    return derive_id(
        CLAIM_FAMILY_PREFIX,
        model_id,
        subject.strip().lower(),
        predicate.strip().lower(),
        object_.strip().lower(),
    )


def claim_id(family_id: str, model_version: str, distribution_version: str) -> str:
    """Identify one claim instance: a family scoped to a model and distribution version."""
    return derive_id(CLAIM_PREFIX, family_id, model_version, distribution_version)


def experiment_id(claim: str, protocol_version: str, seed: int, repetitions: int) -> str:
    """Identify an experiment by everything that determines its result.

    Two requests with identical plans collapse to one experiment, which is what makes
    duplicate events safe at the engine layer rather than only at the bus layer.
    """
    return derive_id(EXPERIMENT_PREFIX, claim, protocol_version, seed, repetitions)


def run_id(experiment: str, kind: str, index: int) -> str:
    return derive_id(RUN_PREFIX, experiment, kind, index)


def evidence_id(experiment: str, output_hash: str) -> str:
    return derive_id(EVIDENCE_PREFIX, experiment, output_hash)


def verdict_id(claim: str, evidence_ids: list[str], verifier_version: str) -> str:
    return derive_id(VERDICT_PREFIX, claim, sorted(evidence_ids), verifier_version)


def idempotency_key(event_type: str, aggregate_id: str, aggregate_version: str) -> str:
    """The unit of 'we have already done this work'.

    Keyed on what the work is about, not on the event envelope, so a redelivered event and
    a genuinely duplicate event from a different producer both collapse to one execution.
    """
    return short_hash([event_type, aggregate_id, aggregate_version], length=24)
