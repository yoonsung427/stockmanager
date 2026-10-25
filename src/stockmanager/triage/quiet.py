"""Quiet hours (v2 R5): nothing pages overnight; quiet-hours items defer to the morning delivery.

There is no separate morning digest job yet (the digest runs at the close), so the *morning delivery* is the moment
quiet hours end. A deferred item's wake becomes due then, which is also when its veto window starts (alert-before-fill).
All times are PT, the system clock zone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta


@dataclass(frozen=True)
class QuietHours:
    """A daily ``[start, end)`` window in the zone of the instants it is asked about; wraps midnight if ``end <= start``."""

    start: time
    end: time

    @classmethod
    def parse(cls, text: str) -> QuietHours:
        """Parse ``HH:MM-HH:MM`` (the ``triage.quiet_hours`` format)."""
        a, b = text.split("-")
        return cls(time.fromisoformat(a), time.fromisoformat(b))

    def contains(self, now: datetime) -> bool:
        """True if ``now`` falls inside the window."""
        t = now.timetz().replace(tzinfo=None)
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    def next_end(self, now: datetime) -> datetime:
        """The first instant at or after ``now`` when quiet hours are over (``now`` itself if it is not quiet)."""
        if not self.contains(now):
            return now
        end = now.replace(hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0)
        if end <= now:
            end += timedelta(days=1)
        return end
