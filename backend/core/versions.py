"""Component versions.

Every artifact Ariadne persists records the version of the code that produced it.
Without this, historical evidence cannot be reinterpreted honestly after a rule change.
"""

from __future__ import annotations

SCHEMA_VERSION = "1.0"
"""Version of the Pydantic domain contracts."""

VERIFIER_VERSION = "1.0.0"
"""Version of the deterministic verifier ruleset. Bump on ANY verdict-rule change."""

PROTOCOL_VERSION = "1.0.0"
"""Version of the intervention protocol (how baseline/intervention/control are built)."""

POLICY_VERSION = "1.0.0"
"""Version of the Explanation Debt weights and Governor thresholds."""

REGISTRY_VERSION = "1.0.0"
"""Version of the agent registry manifest format."""
