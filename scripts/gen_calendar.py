"""Build ``data/calendars/nyse.json`` (AS §6.4). Dev-time only; the output is committed.

Holidays and early closes follow the published NYSE rules, using only the standard library:

* New Year's Day, MLK Day (3rd Mon Jan), Presidents' Day (3rd Mon Feb), Good Friday, Memorial Day (last Mon May),
  Juneteenth (Jun 19), Independence Day (Jul 4), Labor Day (1st Mon Sep), Thanksgiving (4th Thu Nov), Christmas.
* A fixed-date holiday on a Saturday is observed the Friday before and one on a Sunday the Monday after, except
  that New Year's Day on a Saturday is not observed at all (the exchange stays open on Dec 31).
* Early close (13:00 ET): the day after Thanksgiving, Dec 24 and Jul 3 when each is a regular weekday session.

Rule-derived dates should be cross-checked against the exchange's published calendar before each regeneration.
Usage: ``python scripts/gen_calendar.py [first_year] [last_year]``.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent.parent / "data" / "calendars" / "nyse.json"
EARLY = {"open": "09:30", "close": "13:00"}


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Mon=0) of the month; ``n=-1`` is the last."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def easter(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def observed(day: date) -> date:
    """Saturday -> Friday, Sunday -> Monday."""
    return day - timedelta(days=1) if day.weekday() == 5 else day + timedelta(days=1) if day.weekday() == 6 else day


def holidays(year: int) -> set[date]:
    """NYSE full-day closures that fall in ``year``."""
    found = {
        nth_weekday(year, 1, 0, 3),
        nth_weekday(year, 2, 0, 3),
        easter(year) - timedelta(days=2),
        nth_weekday(year, 5, 0, -1),
        nth_weekday(year, 9, 0, 1),
        nth_weekday(year, 11, 3, 4),
        observed(date(year, 6, 19)),
        observed(date(year, 7, 4)),
        observed(date(year, 12, 25)),
    }
    if date(year, 1, 1).weekday() != 5:
        found.add(observed(date(year, 1, 1)))
    # Next year's New Year's Day is never observed on this year's Dec 31, so nothing to add here.
    return {d for d in found if d.year == year}


def build(first_year: int, last_year: int) -> dict[str, object]:
    """The calendar document for ``first_year``..``last_year`` inclusive."""
    closed: set[date] = set()
    for year in range(first_year, last_year + 1):
        closed |= holidays(year)
    early: set[date] = set()
    for year in range(first_year, last_year + 1):
        early |= {nth_weekday(year, 11, 3, 4) + timedelta(days=1), date(year, 12, 24), date(year, 7, 3)}
    early = {d for d in early if d.weekday() < 5 and d not in closed}
    start, end = date(first_year, 1, 2), date(last_year, 12, 29)
    return {
        "schema_version": 1,
        "covers": [start.isoformat(), end.isoformat()],
        "sessions": {d.isoformat(): EARLY for d in sorted(early) if start <= d <= end},
        "holidays": [d.isoformat() for d in sorted(closed) if start <= d <= end],
    }


def main(argv: list[str]) -> None:
    """Write ``data/calendars/nyse.json`` (canonical JSON: indent 2, trailing newline)."""
    first, last = (int(argv[1]), int(argv[2])) if len(argv) == 3 else (2026, 2028)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(build(first, last), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv)
