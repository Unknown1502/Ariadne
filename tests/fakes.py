"""Test doubles.

``FakeFirestore`` mimics the slice of the Firestore client that ``FirestoreRuntimeStore``
uses: documents, subcollections, streaming, and - critically - ``create()`` failing when a
document already exists, which is the atomic primitive the whole idempotency story rests on.

Being explicit about what this does and does not prove:

  - It **does** test the adapter's logic - key layout, serialization, filtering, ordering,
    and the claim/complete/fail/release state machine.
  - It **does not** prove the adapter works against real Firestore. Network behaviour,
    consistency, permissions, and SDK version drift are all outside it.

The second point is why `docs/limitations.md` says the cloud path is unexercised. A double
that quietly stood in for the real thing would be exactly the kind of false assurance this
project exists to argue against.

Values are deep-copied on the way in and out, mimicking a network round-trip. Without that,
a caller mutating a returned dict would silently corrupt the store, and the bug would only
appear against the real client.
"""

from __future__ import annotations

import copy
from typing import Any

from backend.storage.firestore import DocumentExists


class FakeConflict(DocumentExists):
    """What Firestore actually raises from ``create()`` on an existing document.

    Named and shaped to match reality. Firestore maps
    ``grpc.StatusCode.ALREADY_EXISTS -> google.api_core.exceptions.Conflict``, so the error
    the adapter must handle is ``Conflict``, not its ``AlreadyExists`` subclass.

    This distinction is not academic: the adapter originally caught only ``AlreadyExists``,
    and because a subclass handler cannot catch its parent, duplicate-event safety was
    broken on the Firestore path. The whole contract suite passed anyway — because this
    double raised a bare ``DocumentExists`` that the adapter happened to catch, making the
    fake more forgiving than the system it stood in for.

    A double that cannot reproduce the failure it exists to model is worse than no double,
    so this one now mirrors the real hierarchy.
    """


class FakeSnapshot:
    """A document read result."""

    def __init__(self, identifier: str, data: dict[str, Any] | None) -> None:
        self.id = identifier
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data) if self._data is not None else None


class FakeDocument:
    def __init__(self, store: dict[str, Any], identifier: str) -> None:
        self._store = store
        self.id = identifier

    def create(self, data: dict[str, Any]) -> None:
        """Fail if the document exists. This is the atomic claim primitive."""
        if self.id in self._store:
            raise FakeConflict(f"document {self.id!r} already exists")
        self._store[self.id] = {"_data": copy.deepcopy(data), "_sub": {}}

    def set(self, data: dict[str, Any]) -> None:
        existing = self._store.get(self.id, {"_sub": {}})
        self._store[self.id] = {"_data": copy.deepcopy(data), "_sub": existing.get("_sub", {})}

    def get(self) -> FakeSnapshot:
        entry = self._store.get(self.id)
        return FakeSnapshot(self.id, entry["_data"] if entry else None)

    def delete(self) -> None:
        self._store.pop(self.id, None)

    def collection(self, name: str) -> FakeCollection:
        entry = self._store.setdefault(self.id, {"_data": None, "_sub": {}})
        return FakeCollection(entry["_sub"].setdefault(name, {}))


class FakeCollection:
    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store

    def document(self, identifier: str) -> FakeDocument:
        return FakeDocument(self._store, identifier)

    def stream(self):
        # Only documents with data are streamed. A parent created solely to hold a
        # subcollection has no fields of its own, exactly as in Firestore.
        for identifier, entry in list(self._store.items()):
            if entry.get("_data") is not None:
                yield FakeSnapshot(identifier, entry["_data"])


class FakeFirestore:
    """Minimal in-memory stand-in for google.cloud.firestore.Client."""

    def __init__(self) -> None:
        self._collections: dict[str, dict[str, Any]] = {}

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self._collections.setdefault(name, {}))

    def document_count(self, collection: str) -> int:
        return len(self._collections.get(collection, {}))
