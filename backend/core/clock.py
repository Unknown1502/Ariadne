"""Injectable time.

Determinism is a hard requirement, and `datetime.now()` is the most common way to lose it.
Every component takes a Clock; tests use ManualClock so timestamps are reproducible and
validity-window logic can be exercised without sleeping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, always timezone-aware UTC."""
        ...


class SystemClock:
    """Wall-clock time. Used in production paths only."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """Test clock. Time only moves when a test moves it."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        if self._now.tzinfo is None:
            raise ValueError("ManualClock requires a timezone-aware datetime")

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        """Move time forward, e.g. advance(days=45)."""
        self._now = self._now + timedelta(**kwargs)
        return self._now

    def set(self, moment: datetime) -> datetime:
        if moment.tzinfo is None:
            raise ValueError("ManualClock requires a timezone-aware datetime")
        self._now = moment
        return self._now


def utc(value: datetime) -> datetime:
    """Normalize any datetime to timezone-aware UTC.

    Naive datetimes are rejected rather than assumed to be UTC: silently guessing a
    timezone is how validity windows quietly become wrong.
    """
    if value.tzinfo is None:
        raise ValueError("naive datetime is not allowed; supply tzinfo")
    return value.astimezone(UTC)
