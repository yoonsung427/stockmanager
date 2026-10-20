"""``FakeCalendar``: a ``SessionCalendar`` with fixture holidays (TS §0.3, §0.4)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta

from stockmanager.sim.clock import PT

SESSION_OPEN = time(6, 30)


class FakeCalendar:
    """Monday-Friday sessions opening 06:30 PT, minus ``holidays``."""

    def __init__(self, holidays: Iterable[date] = ()) -> None:
        self.holidays = frozenset(holidays)

    def is_session(self, day: date) -> bool:
        """True for a weekday that is not a holiday."""
        return day.weekday() < 5 and day not in self.holidays

    def next_session_open_at_or_after(self, instant: datetime) -> datetime:
        """The 06:30 PT open of the first session whose open is at or after ``instant``."""
        local = instant.astimezone(PT)
        day = local.date()
        for _ in range(21):
            candidate = datetime.combine(day, SESSION_OPEN, tzinfo=PT)
            if self.is_session(day) and candidate >= local:
                return candidate
            day += timedelta(days=1)
        raise ValueError("no session open within 21 days")  # pragma: no cover

    def last_session_on_or_before(self, day: date) -> date:
        """The latest session date <= ``day`` (the valuation date for a Saturday evaluation, TS EDGE-14)."""
        for _ in range(21):
            if self.is_session(day):
                return day
            day -= timedelta(days=1)
        raise ValueError("no session within 21 days")  # pragma: no cover
