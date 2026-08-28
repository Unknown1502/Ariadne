"""Canonical hashing and provenance.

Evidence is only trustworthy if you can prove which bytes produced it. Everything hashed
here goes through one canonical encoder so the same logical value always yields the same
digest, on any platform, in any Python process.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from enum import Enum
from typing import Any

FLOAT_FORMAT = ".12g"
"""Floats are normalized before hashing so 0.1+0.2 and 0.30000000000000004 do not
produce different digests for what is, at experiment precision, the same number."""


def canonicalize(value: Any) -> Any:
    """Convert a value into a canonical, JSON-serializable form.

    Rules:
      - floats are formatted to fixed precision (see FLOAT_FORMAT)
      - NaN and infinities are rejected: they must never enter the evidence ledger
      - dict keys are stringified and sorted by the encoder
      - datetimes become UTC ISO-8601
      - enums become their values
      - tuples and sets become lists (sets are sorted for stability)
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"non-finite float cannot be hashed: {value!r}")
        return format(value, FLOAT_FORMAT)
    if isinstance(value, int | str) or value is None:
        return value
    if isinstance(value, Enum):
        return canonicalize(value.value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime cannot be hashed; supply tzinfo")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {
            str(k): canonicalize(v)
            for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, list | tuple):
        return [canonicalize(v) for v in value]
    if isinstance(value, set | frozenset):
        return sorted((canonicalize(v) for v in value), key=repr)
    if hasattr(value, "model_dump"):
        return canonicalize(value.model_dump(mode="python"))
    raise TypeError(f"cannot canonicalize {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Deterministic JSON encoding: sorted keys, no incidental whitespace."""
    return json.dumps(canonicalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(value: Any) -> str:
    """Full SHA-256 digest of the canonical encoding, prefixed with its algorithm."""
    payload = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def short_hash(value: Any, length: int = 12) -> str:
    """Short digest for identifiers. Not a security primitive - use sha256_hex for provenance."""
    payload = canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:length]


def hash_chain(previous: str | None, value: Any) -> str:
    """Link a new record to its predecessor.

    The evidence ledger is append-only; chaining each entry to the previous digest means a
    silently edited or removed historical row breaks every digest after it.
    """
    return sha256_hex({"prev": previous, "value": canonicalize(value)})
