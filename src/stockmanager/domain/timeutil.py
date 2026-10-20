"""Time helpers: the Pacific zone every timestamp is kept in, and DST-safe elapsed arithmetic (TS EDGE-18)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")


def add_elapsed(moment: datetime, delta: timedelta) -> datetime:
    """``moment`` plus ``delta`` of *elapsed* time, in ``moment``'s own zone.

    Aware-datetime ``+`` on a ``ZoneInfo`` zone adds wall-clock time, so 24 h from Saturday 09:00 PDT lands on
    Sunday 09:00 PST (25 h later). Going through UTC counts real hours.
    """
    if moment.tzinfo is None:
        raise ValueError("add_elapsed needs a timezone-aware datetime")
    return (moment.astimezone(UTC) + delta).astimezone(moment.tzinfo)
