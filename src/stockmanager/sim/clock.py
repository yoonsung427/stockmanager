"""Clock and PID-checker doubles (TS §0.3): settable, steppable, America/Los_Angeles."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")


class FakeClock:
    """Clock seam: calling it returns ``now``; tests set or step it. Time never moves on its own."""

    def __init__(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware instant")
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, now: datetime) -> None:
        """Jump to ``now`` (forwards or backwards)."""
        self.now = now

    def advance(self, **kwargs: float) -> None:
        """Step forwards by a ``timedelta`` built from ``kwargs`` (``minutes=16``)."""
        self.now += timedelta(**kwargs)


class FakePidChecker:
    """``PidChecker`` seam. ``alive_pids`` maps pid -> start time of the process now owning that pid; default: all dead."""

    def __init__(self, default_start: datetime | None = None) -> None:
        self.alive_pids: dict[int, datetime] = {}
        self._default_start = default_start or datetime(2026, 1, 1, tzinfo=PT)

    def alive(self, pid: int, start: datetime) -> bool:
        """True iff ``pid`` is registered and started at ``start`` (2 s tolerance, as the real checker)."""
        return pid in self.alive_pids and abs(self.alive_pids[pid] - start).total_seconds() <= 2

    def start_time(self, pid: int) -> datetime:
        """The registered start time of ``pid``, else a fixed instant."""
        return self.alive_pids.get(pid, self._default_start)
