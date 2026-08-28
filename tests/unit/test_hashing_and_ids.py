"""Provenance primitives.

Evidence integrity rests entirely on these two modules. If hashing is unstable, the
lineage chain produces false tamper alarms; if it is *too* stable (collapsing genuinely
different inputs), tampering goes undetected. Both directions are tested here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from backend.core.hashing import canonical_json, hash_chain, sha256_hex, short_hash
from backend.core.ids import (
    claim_family_id,
    claim_id,
    derive_id,
    experiment_id,
    idempotency_key,
    random_id,
)


class TestCanonicalisation:
    def test_key_order_does_not_change_the_digest(self) -> None:
        assert sha256_hex({"a": 1, "b": 2}) == sha256_hex({"b": 2, "a": 1})

    def test_float_representation_error_does_not_change_the_digest(self) -> None:
        assert sha256_hex({"x": 0.1 + 0.2}) == sha256_hex({"x": 0.3})

    def test_equal_instants_in_different_timezones_hash_alike(self) -> None:
        utc_noon = datetime(2026, 1, 1, 12, tzinfo=UTC)
        ist_same = datetime(2026, 1, 1, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        assert sha256_hex(utc_noon) == sha256_hex(ist_same)

    def test_naive_datetimes_are_refused(self) -> None:
        with pytest.raises(ValueError, match="naive datetime"):
            sha256_hex(datetime(2026, 1, 1))

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_values_never_enter_the_ledger(self, bad: float) -> None:
        with pytest.raises(ValueError, match="non-finite"):
            sha256_hex({"score": bad})

    def test_different_values_produce_different_digests(self) -> None:
        assert sha256_hex({"x": 0.30001}) != sha256_hex({"x": 0.3})
        assert sha256_hex({"a": 1}) != sha256_hex({"a": "1"})

    def test_nesting_is_not_flattened_away(self) -> None:
        assert sha256_hex({"a": {"b": 1}}) != sha256_hex({"a.b": 1})

    def test_list_order_is_significant(self) -> None:
        assert sha256_hex([1, 2]) != sha256_hex([2, 1])

    def test_output_is_algorithm_tagged(self) -> None:
        assert sha256_hex({"a": 1}).startswith("sha256:")

    def test_encoding_is_compact_and_sorted(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_pydantic_models_are_hashable(self, scope) -> None:
        assert sha256_hex(scope) == sha256_hex(scope.model_copy())

    def test_unsupported_types_are_refused_rather_than_coerced(self) -> None:
        with pytest.raises(TypeError, match="cannot canonicalize"):
            sha256_hex({"f": object()})


class TestHashChain:
    def test_chain_depends_on_its_ancestor(self) -> None:
        first = hash_chain(None, {"v": 1})
        second = hash_chain(first, {"v": 2})
        forged = hash_chain(None, {"v": 2})
        assert second != forged

    def test_altering_an_ancestor_breaks_every_descendant(self) -> None:
        genuine = hash_chain(hash_chain(None, {"v": 1}), {"v": 2})
        tampered = hash_chain(hash_chain(None, {"v": 999}), {"v": 2})
        assert genuine != tampered

    def test_chain_is_reproducible(self) -> None:
        assert hash_chain("sha256:abc", {"v": 1}) == hash_chain("sha256:abc", {"v": 1})


class TestIdentifiers:
    def test_content_addressed_ids_are_reproducible(self) -> None:
        assert derive_id("EXP", "a", 1) == derive_id("EXP", "a", 1)

    def test_different_content_yields_a_different_id(self) -> None:
        assert derive_id("EXP", "a", 1) != derive_id("EXP", "a", 2)

    def test_claim_family_ignores_version_so_lineage_can_follow_it(self) -> None:
        # The whole temporal story depends on this: one family, many versions.
        family = claim_family_id("m", "urgency_marker", "is_primary_driver", "priority_score")
        v1 = claim_id(family, "1.0.0", "d1")
        v2 = claim_id(family, "2.0.0", "d1")
        assert v1 != v2
        assert (
            claim_family_id("m", "urgency_marker", "is_primary_driver", "priority_score")
            == family
        )

    def test_claim_family_normalises_case_and_whitespace(self) -> None:
        assert claim_family_id("m", " Urgency_Marker ", "IS_PRIMARY_DRIVER", "priority_score") == (
            claim_family_id("m", "urgency_marker", "is_primary_driver", "priority_score")
        )

    def test_claim_id_separates_distributions(self) -> None:
        family = claim_family_id("m", "s", "p", "o")
        assert claim_id(family, "1.0.0", "d1") != claim_id(family, "1.0.0", "d2")

    def test_identical_plans_collapse_to_one_experiment_id(self) -> None:
        # This is what makes a duplicate event safe at the engine layer, not just the bus.
        assert experiment_id("CLM-1", "1.0.0", 7, 12) == experiment_id("CLM-1", "1.0.0", 7, 12)

    def test_a_different_seed_is_a_different_experiment(self) -> None:
        assert experiment_id("CLM-1", "1.0.0", 7, 12) != experiment_id("CLM-1", "1.0.0", 8, 12)

    def test_idempotency_key_is_a_function_of_the_work(self) -> None:
        first = idempotency_key("MODEL_VERSION_DEPLOYED", "triage", "2.0.0")
        second = idempotency_key("MODEL_VERSION_DEPLOYED", "triage", "2.0.0")
        assert first == second
        assert first != idempotency_key("MODEL_VERSION_DEPLOYED", "triage", "3.0.0")
        assert first != idempotency_key("DISTRIBUTION_CHANGED", "triage", "2.0.0")

    def test_random_ids_are_unique(self) -> None:
        assert len({random_id("EVT") for _ in range(1000)}) == 1000

    def test_short_hash_length_is_configurable(self) -> None:
        assert len(short_hash("x", length=8)) == 8
