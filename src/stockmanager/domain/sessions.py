"""Which trading session a moment's prices belong to (AS §6.4 ``latest_completed_session``; TS EDGE-01, EDGE-14).

Derived from the ``SessionCalendar`` port alone: a day is a session iff the first open at or after its midnight
falls on that day. Early closes are not modelled by the port, so the regular 13:00 PT close is used; a provider
that has not settled the bar yet fails closed on its own (``incomplete_session``, AS §6.1).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from stockmanager.domain.ports import SessionCalendar
from stockmanager.domain.timeutil import PT

REGULAR_CLOSE = time(13, 0)
SETTLE = timedelta(minutes=20)
_HORIZON_DAYS = 21


def is_session(calendar: SessionCalendar, day: date) -> bool:
    """True iff ``day`` has a regular session."""
    return calendar.next_session_open_at_or_after(datetime.combine(day, time(0, 0), tzinfo=PT)).date() == day


def last_completed_session(calendar: SessionCalendar, now: datetime) -> date:
    """The latest session whose close, plus the settle margin, is at or before ``now``.

    A Saturday 09:00 evaluation therefore prices Friday's close, and Saturday 2026-12-26 prices Thursday 12-24
    because Friday 12-25 is closed. Mid-session, the answer is the previous session.
    """
    local = now.astimezone(PT)
    day = local.date()
    if datetime.combine(day, REGULAR_CLOSE, tzinfo=PT) + SETTLE > local:
        day -= timedelta(days=1)
    for _ in range(_HORIZON_DAYS):
        if is_session(calendar, day):
            return day
        day -= timedelta(days=1)
    raise ValueError(f"no completed session within {_HORIZON_DAYS} days of {now.isoformat()}")  # pragma: no cover
