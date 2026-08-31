"""Which model does an experiment actually run against?

The worst bug this system could have is not a wrong verdict. It is a *confident* verdict
about the wrong model: an organisation registers their endpoint, an event arrives, an
experiment runs against the built-in laboratory instead, and evidence is recorded and scoped
as though it described their model. Every downstream guarantee - lineage, re-audit, the
append-only ledger - would then be faithfully preserving a measurement of something else.

`ExperimentRunner`'s factory took `(version, distribution)` and never received `model_id`,
so it could not tell one model from another even in principle. These tests pin the fix: the
resolver dispatches on the model's identity, and refuses rather than substitutes.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator

import pytest

from backend.core.errors import ValidationError


@pytest.fixture
def store() -> Iterator[object]:
    from backend.config import reset_settings_cache

    previous = os.environ.get("VAR_DIR")
    os.environ["VAR_DIR"] = tempfile.mkdtemp()
    reset_settings_cache()
    try:
        from backend.storage.runtime import open_runtime_store

        yield open_runtime_store()
    finally:
        if previous is None:
            os.environ.pop("VAR_DIR", None)
        else:
            os.environ["VAR_DIR"] = previous
        reset_settings_cache()


def register(store, **overrides):
    from datetime import UTC, datetime

    from backend.core.configuration import RegisteredModel

    moment = datetime.now(UTC)
    model = RegisteredModel(
        id="MDL-test",
        model_id=overrides.pop("model_id", "customer-model"),
        name="Customer model",
        created_at=moment,
        updated_at=moment,
        **overrides,
    )
    store.save_model(model)
    return model


class TestTheLaboratoryStillResolves:
    def test_the_builtin_model_is_unchanged(self, store) -> None:
        """The demo and every existing test depend on this path."""
        from backend.experiment_engine.target_model import MODEL_ID
        from backend.integrations.resolver import resolve_target_model

        model = resolve_target_model(MODEL_ID, "2.0.0", "baseline_2024.1", runtime=store)
        assert model.version == "2.0.0"
        assert model.model_id == MODEL_ID

    def test_an_unregistered_unknown_model_is_refused(self, store) -> None:
        """Not silently the laboratory. An unknown id is a configuration error."""
        from backend.integrations.resolver import resolve_target_model

        with pytest.raises(ValidationError, match="not registered"):
            resolve_target_model(
                "nobody-registered-this", "2.0.0", "baseline_2024.1", runtime=store
            )


class TestARegisteredModelIsNeverSubstituted:
    """The failure this file exists to prevent."""

    def test_a_registered_model_without_a_connection_is_refused(self, store) -> None:
        register(store)
        from backend.integrations.resolver import resolve_target_model

        with pytest.raises(ValidationError, match="no model-endpoint connection"):
            resolve_target_model("customer-model", "1.0.0", "prod", runtime=store)

    def test_a_registered_model_with_a_dead_connection_is_refused(self, store) -> None:
        """A failed endpoint must stop the experiment, not redirect it."""
        from datetime import UTC, datetime

        from backend.core.configuration import Connection, ConnectionKind, TransportKind
        from backend.integrations.resolver import resolve_target_model

        moment = datetime.now(UTC)
        connection = Connection(
            id="CON-dead",
            kind=ConnectionKind.MODEL_ENDPOINT,
            name="Down",
            transport=TransportKind.HTTP,
            endpoint="https://unreachable.invalid/predict",
            created_at=moment,
            updated_at=moment,
        )
        store.save_connection(connection)
        register(store, connection_id="CON-dead")

        with pytest.raises(ValidationError, match="not live"):
            resolve_target_model("customer-model", "1.0.0", "prod", runtime=store)

    def test_a_registered_model_never_resolves_to_the_laboratory(self, store) -> None:
        """The specific substitution that would corrupt the evidence ledger."""
        from backend.experiment_engine.target_model import SyntheticTriageModel
        from backend.integrations.resolver import resolve_target_model

        register(store)
        try:
            model = resolve_target_model("customer-model", "1.0.0", "prod", runtime=store)
        except ValidationError:
            return  # refusing is the correct outcome
        assert not isinstance(model, SyntheticTriageModel), (
            "a registered customer model resolved to the built-in laboratory; every verdict "
            "produced from it would describe the wrong model"
        )


class TestTheRunnerUsesTheResolver:
    def test_the_runner_passes_model_id_to_its_factory(self) -> None:
        """The factory could not previously tell one model from another."""
        import inspect

        from backend.experiment_engine.runner import ExperimentRunner

        source = inspect.getsource(ExperimentRunner._resolve_model)
        assert "model_id" in source, (
            "the runner resolves a model without reference to its identity, so it cannot "
            "distinguish a customer's model from the laboratory"
        )
