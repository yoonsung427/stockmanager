"""JsonCalendar: holidays, early closes, coverage (AS §6.4; TS CAL-2026-27, EDGE-04/05/13/14)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from stockmanager.adapters.calendar import CalendarCoverageError, JsonCalendar
from stockmanager.domain.sessions import last_completed_session
from stockmanager.domain.timeutil import PT


@pytest.fixture(scope="module")
def cal() -> JsonCalendar:
    return JsonCalendar.load()


def at(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=PT)


@pytest.mark.parametrize(
    "day",
    [date(2026, 11, 26), date(2026, 12, 25), date(2027, 1, 1), date(2027, 1, 18), date(2026, 7, 3), date(2027, 3, 26)],
)
def test_holidays_are_not_sessions(cal: JsonCalendar, day: date) -> None:
    assert not cal.is_session(day)


def test_weekends_are_not_sessions(cal: JsonCalendar) -> None:
    assert not cal.is_session(date(2026, 10, 24)) and not cal.is_session(date(2026, 10, 25))


def test_ordinary_weekday_is_a_session_with_regular_hours(cal: JsonCalendar) -> None:
    day = date(2026, 10, 21)
    assert cal.is_session(day) and not cal.is_early_close(day)
    assert cal.session_open(day) == at(2026, 10, 21, 6, 30)
    assert cal.session_close(day) == at(2026, 10, 21, 13, 0)


@pytest.mark.parametrize("day", [date(2026, 11, 27), date(2026, 12, 24)])
def test_early_close_ends_at_1pm_et(cal: JsonCalendar, day: date) -> None:
    assert cal.is_session(day) and cal.is_early_close(day)
    assert cal.session_close(day) == datetime.combine(day, datetime.min.time(), tzinfo=PT).replace(hour=10)
    assert cal.session_open(day).hour == 6 and cal.session_open(day).minute == 30


def test_session_hours_for_a_holiday_raise(cal: JsonCalendar) -> None:
    with pytest.raises(ValueError):
        cal.session_open(date(2026, 12, 25))


def test_dst_change_moves_the_et_open_not_the_pt_open(cal: JsonCalendar) -> None:
    # US DST ended 2026-11-01 for both zones on the same day, so 09:30 ET is 06:30 PT on both sides.
    assert cal.session_open(date(2026, 10, 30)).hour == 6
    assert cal.session_open(date(2026, 11, 2)).hour == 6


def test_next_open_skips_holiday_weekend(cal: JsonCalendar) -> None:
    # HOLIDAY_EVAL: Sat 2027-01-16 09:00 -> Mon 01-18 is a holiday -> Tue 01-19 06:30 PT (EDGE-13)
    assert cal.next_session_open_at_or_after(at(2027, 1, 16, 9)) == at(2027, 1, 19, 6, 30)


def test_next_open_after_christmas(cal: JsonCalendar) -> None:
    assert cal.next_session_open_at_or_after(at(2026, 12, 24, 12)) == at(2026, 12, 28, 6, 30)


def test_next_open_is_inclusive_of_the_open_instant(cal: JsonCalendar) -> None:
    assert cal.next_session_open_at_or_after(at(2026, 10, 21, 6, 30)) == at(2026, 10, 21, 6, 30)
    assert cal.next_session_open_at_or_after(at(2026, 10, 21, 6, 31)) == at(2026, 10, 22, 6, 30)


def test_valuation_date_on_xmas_eval_is_the_early_close_day(cal: JsonCalendar) -> None:
    # XMAS_EVAL (EDGE-14): Sat 2026-12-26 09:00 values Thu 12-24; Fri 12-25 is closed.
    assert last_completed_session(cal, at(2026, 12, 26, 9)) == date(2026, 12, 24)


def test_outside_coverage_fails_closed(cal: JsonCalendar) -> None:
    with pytest.raises(CalendarCoverageError):
        cal.is_session(date(2025, 6, 2))
    with pytest.raises(CalendarCoverageError):
        cal.next_session_open_at_or_after(at(2029, 1, 5))


def test_covered_year_has_plausible_session_count(cal: JsonCalendar) -> None:
    days = [date(2027, 1, 1) + timedelta(days=i) for i in range(365)]
    assert 250 <= sum(cal.is_session(d) for d in days) <= 253  # NYSE trades 251-252 days a year


def test_unsupported_schema_version_is_rejected() -> None:
    with pytest.raises(ValueError):
        JsonCalendar({"schema_version": 2, "covers": ["2026-01-02", "2026-12-29"], "sessions": {}, "holidays": []})
