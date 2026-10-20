"""A weekday ``SessionCalendar`` (AS §6.4), used until a holiday-aware ``data/calendars/nyse.json`` is generated.

The architecture spec calls for a JSON-backed calendar built offline from an exchange-calendar library
(``scripts/gen_calendar.py`` -> ``data/calendars/nyse.json``); that generator has not been built yet. This adapter
is a deliberately simple stand-in: every Monday-Friday is a session opening at 06:30 PT, with no NYSE holiday or
early-close awareness. It satisfies the ``SessionCalendar`` protocol so the rest of the system (execution,
evaluation, proposal timing) can be wired and exercised end to end; swap in the JSON-backed calendar without
touching any caller once it exists.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
SESSION_OPEN = time(6, 30)
_SEARCH_HORIZON_DAYS = 14


class SimpleWeekdayCalendar:
    """``SessionCalendar``: Monday-Friday, 06:30 PT open, no holiday calendar (see module docstring)."""

    def next_session_open_at_or_after(self, instant: datetime) -> datetime:
        """Return the 06:30 PT open of the first weekday whose open is at or after ``instant``."""
        local = instant.astimezone(PT)
        day = local.date()
        for _ in range(_SEARCH_HORIZON_DAYS):
            candidate = datetime.combine(day, SESSION_OPEN, tzinfo=PT)
            if day.weekday() < 5 and candidate >= local:
                return candidate
            day += timedelta(days=1)
        raise ValueError("no session open found within the search horizon")  # pragma: no cover
