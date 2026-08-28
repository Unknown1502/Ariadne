"""RuntimeStateStore must declare everything callers actually use.

Same pattern as `test_bus_protocol_parity.py`, for the other protocol that let a real bug
through: `RuntimeStateStore` was missing `stats()` and `all_audits()`, so
`backend/api/main.py`'s calls to them type-checked against `LocalRuntimeStore` (the concrete
class it used to hardcode) but would have failed the moment the runtime store construction
was fixed to go through the protocol-returning factory - exactly what happened. mypy caught
it only because the AppState fix (using `open_runtime_store()`) made the return type
`RuntimeStateStore` instead of the concrete class for the first time.
"""

from __future__ import annotations

import inspect

import pytest

from backend.storage.firestore import FirestoreRuntimeStore
from backend.storage.runtime import LocalRuntimeStore, RuntimeStateStore

IMPLEMENTATIONS = [LocalRuntimeStore, FirestoreRuntimeStore]
PROTOCOL_MEMBERS = sorted(name for name in dir(RuntimeStateStore) if not name.startswith("_"))


class TestProtocolIsComplete:
    def test_the_protocol_covers_what_main_py_actually_calls(self) -> None:
        for required in (
            "claim", "get_idempotency", "complete", "fail", "release",
            "save_investigation", "get_investigation", "list_investigations",
            "record_run", "completed_runs",
            "schedule_audit", "due_audits", "mark_audit_executed",
            "save_approval", "get_approval", "pending_approvals",
            "all_audits", "stats",
        ):
            assert required in PROTOCOL_MEMBERS, f"{required} dropped from RuntimeStateStore"


@pytest.mark.parametrize("implementation", IMPLEMENTATIONS, ids=lambda c: c.__name__)
class TestEveryImplementationConforms:
    def test_every_protocol_member_exists(self, implementation: type) -> None:
        missing = [name for name in PROTOCOL_MEMBERS if not hasattr(implementation, name)]
        assert not missing, f"{implementation.__name__} is missing {missing}"

    def test_sync_shape_matches_the_protocol(self, implementation: type) -> None:
        # Both implementations are entirely synchronous; guard against one drifting async
        # under a caller that never awaits it.
        mismatches = [
            name
            for name in PROTOCOL_MEMBERS
            if inspect.iscoroutinefunction(inspect.getattr_static(implementation, name))
        ]
        assert not mismatches, f"{implementation.__name__} has unexpectedly async: {mismatches}"


class TestOrchestratorAndWorkerAcceptTheProtocol:
    """The other half of the bug: even a complete protocol is useless if callers pin their
    parameter type to one concrete implementation instead of the protocol itself."""

    def test_build_pipeline_is_typed_against_the_protocol(self) -> None:
        import backend.runtime.orchestrator as orch

        sig = inspect.signature(orch.build_pipeline)
        annotation = sig.parameters["runtime"].annotation
        assert annotation in ("RuntimeStateStore", RuntimeStateStore), (
            f"build_pipeline's `runtime` parameter is typed {annotation!r}; pinning it to "
            f"one concrete store silently rejects the other at type-check time"
        )

    def test_ariadneworker_is_typed_against_the_protocol(self) -> None:
        import backend.runtime.worker as worker

        sig = inspect.signature(worker.AriadneWorker.__init__)
        annotation = sig.parameters["runtime"].annotation
        assert annotation in ("RuntimeStateStore", RuntimeStateStore), (
            f"AriadneWorker's `runtime` parameter is typed {annotation!r}; pinning it to "
            f"one concrete store silently rejects the other at type-check time"
        )
