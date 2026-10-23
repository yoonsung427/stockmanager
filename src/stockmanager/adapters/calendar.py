"""The JSON-backed NYSE ``SessionCalendar`` (AS §6.4), reading ``data/calendars/nyse.json``.

The file is generated offline by ``scripts/gen_calendar.py`` and committed, so there is no runtime dependency on an
exchange-calendar library. It lists the holidays and the early-close sessions (times in America/New_York) for the
range it ``covers``; every other Monday-Friday in that range is a regular 09:30-16:00 ET session. Asking about a day
outside ``covers`` fails closed with ``CalendarCoverageError`` rather than guessing.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from stockmanager.domain.timeutil import PT

__all__ = ["DEFAULT_CALENDAR_PATH", "CalendarCoverageError", "JsonCalendar"]

NY = ZoneInfo("America/New_York")
DEFAULT_CALENDAR_PATH = Path(__file__).resolve().parents[3] / "data" / "calendars" / "nyse.json"
_REGULAR_OPEN = time(9, 30)
_REGULAR_CLOSE = time(16, 0)
_SCHEMA_VERSION = 1


class CalendarCoverageError(ValueError):
    """A date lies outside the range the calendar file covers (jobs that need sessions fail closed)."""


def _hhmm(text: str) -> time:
    return time.fromisoformat(text)


class JsonCalendar:
    """``SessionCalendar`` backed by ``nyse.json``: weekends and holidays are closed, early closes are honoured."""

    def __init__(self, data: dict[str, Any]) -> None:
        if data.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError(f"unsupported calendar schema_version: {data.get('schema_version')!r}")
        first, last = data["covers"]
        self.first = date.fromisoformat(first)
        self.last = date.fromisoformat(last)
        self._holidays = frozenset(date.fromisoformat(d) for d in data["holidays"])
        self._special = {
            date.fromisoformat(d): (_hhmm(hours["open"]), _hhmm(hours["close"])) for d, hours in data["sessions"].items()
        }

    @classmethod
    def load(cls, path: Path = DEFAULT_CALENDAR_PATH) -> JsonCalendar:
        """Read the calendar file at ``path``."""
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def _check(self, day: date) -> None:
        if not self.first <= day <= self.last:
            raise CalendarCoverageError(f"{day} is outside the calendar's coverage {self.first}..{self.last}")

    def is_session(self, day: date) -> bool:
        """True iff the exchange trades on ``day``."""
        self._check(day)
        return day.weekday() < 5 and day not in self._holidays

    def is_early_close(self, day: date) -> bool:
        """True iff ``day`` is a session that closes before 16:00 ET."""
        return self.is_session(day) and self._special.get(day, (None, _REGULAR_CLOSE))[1] != _REGULAR_CLOSE

    def session_open(self, day: date) -> datetime:
        """The open of the ``day`` session as a PT instant."""
        return self._instant(day, 0)

    def session_close(self, day: date) -> datetime:
        """The close of the ``day`` session as a PT instant (13:00 ET on early closes)."""
        return self._instant(day, 1)

    def _instant(self, day: date, index: int) -> datetime:
        if not self.is_session(day):
            raise ValueError(f"{day} is not a trading session")
        hours = self._special.get(day, (_REGULAR_OPEN, _REGULAR_CLOSE))
        return datetime.combine(day, hours[index], tzinfo=NY).astimezone(PT)

    def next_session_open_at_or_after(self, instant: datetime) -> datetime:
        """The open of the first session whose open is at or after ``instant``."""
        day = instant.astimezone(PT).date() - timedelta(days=1)  # PT date can trail the ET date by a day
        while True:
            day += timedelta(days=1)
            if self.is_session(day) and (opens := self.session_open(day)) >= instant:
                return opens
