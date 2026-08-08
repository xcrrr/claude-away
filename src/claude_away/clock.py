"""Injectable time source.

Every timestamp in Claude Away is timezone-aware UTC, and no business logic calls
``datetime.now()`` directly. Two reasons, both of which matter for a system that is
supposed to survive multi-day unattended runs:

* **Determinism in tests.** Lease expiry, retry budgets and reset windows are all
  time-dependent. Tests must be able to step time forward instantly and exactly rather
  than sleeping and hoping.
* **A single conversion point.** Timestamps are persisted as ISO-8601 UTC strings with a
  ``Z``-equivalent offset, so the database is human-readable and lexicographically
  sortable. Doing that conversion in one place stops naive datetimes leaking into storage.

Storing text rather than epoch floats is a deliberate trade: it costs a few bytes and
buys a state database an operator can read and diff during an incident.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

__all__ = ["Clock", "ManualClock", "SystemClock", "ensure_utc", "parse_timestamp", "to_iso"]


class Clock(Protocol):
    """Anything that can report the current UTC time."""

    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC ``datetime``."""
        ...


class SystemClock:
    """Real wall-clock time, always timezone-aware UTC."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def __repr__(self) -> str:
        return "SystemClock()"


class ManualClock:
    """A controllable clock for tests and simulations.

    Time only moves when the test moves it, which makes lease-expiry and retry-window
    behaviour exactly reproducible instead of flaky.
    """

    __slots__ = ("_now",)

    def __init__(self, start: datetime | None = None) -> None:
        self._now = (
            ensure_utc(start) if start is not None else datetime(2026, 1, 1, tzinfo=timezone.utc)
        )

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0.0, **kwargs: float) -> datetime:
        """Move time forward. Accepts ``timedelta`` keyword arguments for readability."""
        delta = timedelta(seconds=seconds, **kwargs)
        if delta.total_seconds() < 0:
            raise ValueError("ManualClock cannot move backwards")
        self._now = self._now + delta
        return self._now

    def set(self, moment: datetime) -> datetime:
        """Jump to an absolute moment. Refuses to move backwards."""
        moment = ensure_utc(moment)
        if moment < self._now:
            raise ValueError("ManualClock cannot move backwards")
        self._now = moment
        return self._now

    def __repr__(self) -> str:
        return f"ManualClock({self._now.isoformat()})"


def ensure_utc(moment: datetime) -> datetime:
    """Coerce a ``datetime`` to timezone-aware UTC.

    A naive datetime is rejected rather than assumed to be UTC. Guessing a timezone is
    how systems silently drift by hours, and a scheduler that miscalculates a reset
    window is worse than one that fails loudly.
    """
    if moment.tzinfo is None:
        raise ValueError("naive datetime is not accepted; provide a timezone-aware value")
    return moment.astimezone(timezone.utc)


def to_iso(moment: datetime) -> str:
    """Render a UTC timestamp for storage.

    The format is fixed-width with microseconds and a ``+00:00`` offset so that string
    ordering matches chronological ordering, which lets SQLite compare timestamps
    directly without a conversion function.
    """
    return ensure_utc(moment).isoformat(timespec="microseconds")


def parse_timestamp(value: str) -> datetime:
    """Parse a timestamp previously written by :func:`to_iso`."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"stored timestamp {value!r} has no timezone information")
    return parsed.astimezone(timezone.utc)
