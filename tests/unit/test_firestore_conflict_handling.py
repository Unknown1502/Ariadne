"""The Firestore claim path must treat a real Conflict as "already claimed".

Found by checking `DocumentReference.create()` against the live google-cloud-firestore
source rather than against memory. Firestore maps the gRPC status explicitly:

    _GRPC_ERROR_MAPPING = {grpc.StatusCode.ALREADY_EXISTS: exceptions.Conflict, ...}

so `create()` raises `Conflict` — the *parent*. The adapter caught `AlreadyExists`, which is
a *subclass*, and catching a subclass does not catch the parent.

The consequence was as bad as it gets for this project: on the Firestore path, the second
worker racing for an idempotency key received an unhandled exception instead of the `None`
meaning "someone else already has this". Duplicate-event safety — the guarantee Ariadne
demonstrates on stage — was broken in the cloud while every local test passed, because the
local store uses `O_CREAT|O_EXCL` and never goes near this code.

These doubles reproduce the real class hierarchy from google.api_core.exceptions:

    GoogleAPICallError -> ClientError -> Conflict -> AlreadyExists
"""

from __future__ import annotations

import pytest

from backend.core.clock import ManualClock
from backend.storage.firestore import DocumentExists, FirestoreRuntimeStore, _conflict_errors
from tests.factories import T0


class GoogleAPICallError(Exception):
    pass


class ClientError(GoogleAPICallError):
    pass


class Conflict(ClientError):
    """What Firestore actually raises from create()."""


class AlreadyExists(Conflict):
    """A subclass of Conflict. Catching this does NOT catch a raised Conflict."""


class ConflictingDoc:
    """A document reference whose create() always conflicts, like an existing document."""

    def __init__(self, error: type[BaseException]) -> None:
        self._error = error

    def create(self, data: dict) -> None:
        raise self._error("document already exists")

    def set(self, data: dict) -> None:
        pass

    def get(self):
        return type("Snap", (), {"exists": False, "to_dict": lambda self: None})()

    def delete(self) -> None:
        pass


class ConflictingCollection:
    def __init__(self, error: type[BaseException]) -> None:
        self._error = error

    def document(self, key: str) -> ConflictingDoc:
        return ConflictingDoc(self._error)

    def stream(self):
        return iter(())


class ConflictingClient:
    def __init__(self, error: type[BaseException]) -> None:
        self._error = error

    def collection(self, name: str) -> ConflictingCollection:
        return ConflictingCollection(self._error)


class TestTheHierarchyItself:
    def test_already_exists_is_a_subclass_of_conflict(self) -> None:
        assert issubclass(AlreadyExists, Conflict)

    def test_catching_the_subclass_does_not_catch_the_parent(self) -> None:
        # The whole bug in three lines.
        with pytest.raises(Conflict):
            try:
                raise Conflict("document already exists")
            except AlreadyExists:  # pragma: no cover - never taken, which is the point
                pytest.fail("a subclass handler must not catch its parent")


class TestClaimHandlesRealConflicts:
    def _store(self, error: type[BaseException]) -> FirestoreRuntimeStore:
        return FirestoreRuntimeStore(ConflictingClient(error), clock=ManualClock(T0))

    def test_a_conflict_means_already_claimed_not_a_crash(self, monkeypatch) -> None:
        # The regression. Before the fix this raised instead of returning None.
        monkeypatch.setattr(
            "backend.storage.firestore._conflict_errors",
            lambda: (DocumentExists, Conflict),
        )
        assert self._store(Conflict).claim("key-1", "worker-a") is None

    def test_an_already_exists_subclass_is_also_handled(self, monkeypatch) -> None:
        # Catching Conflict is the wider catch, so the subclass is covered too.
        monkeypatch.setattr(
            "backend.storage.firestore._conflict_errors",
            lambda: (DocumentExists, Conflict),
        )
        assert self._store(AlreadyExists).claim("key-1", "worker-a") is None

    def test_an_unrelated_error_still_propagates(self, monkeypatch) -> None:
        # Only conflicts mean "already claimed". A permission or network failure must not be
        # silently reported as a successful skip.
        monkeypatch.setattr(
            "backend.storage.firestore._conflict_errors",
            lambda: (DocumentExists, Conflict),
        )
        with pytest.raises(PermissionError):
            self._store(PermissionError).claim("key-1", "worker-a")


class TestResolvedErrorTuple:
    def test_the_double_sentinel_is_always_present(self) -> None:
        assert DocumentExists in _conflict_errors()

    def test_conflict_is_preferred_over_already_exists_when_the_sdk_is_installed(self) -> None:
        # Skips cleanly without the gcp extra; asserts the real thing when it is available.
        google_errors = pytest.importorskip("google.api_core.exceptions")
        resolved = _conflict_errors()
        assert google_errors.Conflict in resolved, (
            "must catch Conflict, which is what Firestore raises; AlreadyExists alone "
            "would miss it"
        )
        # And because AlreadyExists subclasses Conflict, it is covered automatically.
        assert issubclass(google_errors.AlreadyExists, google_errors.Conflict)
