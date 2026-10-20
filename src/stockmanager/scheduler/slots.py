"""Evaluation slot arithmetic (AS §1.5): wall-clock ``America/Los_Angeles`` via ``zoneinfo``, DST-safe.

Slot *days* are computed in PT local time, so 09:00 stays 09:00 across a DST transition (durations, by contrast,
are added to UTC instants elsewhere, e.g. veto windows -- TS EDGE-18). ``biweekly`` alternates weeks, with parity
anchored to ``state/scheduler.json.anchor_date`` (the date of the first completed evaluation).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from stockmanager.config.model import Config
from stockmanager.scheduler.state import SchedulerState

PT = ZoneInfo("America/Los_Angeles")

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_SEARCH_HORIZON_DAYS = 366


def _eval_time(cfg: Config) -> time:
    hh, mm = (int(x) for x in cfg.evaluation_time.split(":"))
    return time(hh, mm)


def _slot_on(day: date, cfg: Config) -> datetime:
    return datetime.combine(day, _eval_time(cfg), tzinfo=PT)


def _is_slot_day(day: date, cfg: Config, anchor: date | None) -> bool:
    if cfg.evaluation_frequency == "daily":
        return True
    if day.weekday() != _WEEKDAYS[cfg.evaluation_day]:
        return False
    if cfg.evaluation_frequency == "weekly":
        return True
    # biweekly: every second occurrence, parity anchored to `anchor` (AS §1.5)
    if anchor is None:
        return True  # no anchor yet: every occurrence is eligible until the first one completes
    return ((day - anchor).days // 7) % 2 == 0


def latest_slot(now: datetime, cfg: Config, anchor: date | None) -> datetime:
    """The latest scheduled slot <= ``now`` (AS §1.5)."""
    now_pt = now.astimezone(PT)
    day = now_pt.date()
    for _ in range(_SEARCH_HORIZON_DAYS):
        if _is_slot_day(day, cfg, anchor):
            candidate = _slot_on(day, cfg)
            if candidate <= now_pt:
                return candidate
        day -= timedelta(days=1)
    raise ValueError("no evaluation slot found within a year before `now`")  # pragma: no cover


def next_slot(now: datetime, cfg: Config, anchor: date | None) -> datetime:
    """The next scheduled slot >= ``now`` (AS §1.5)."""
    now_pt = now.astimezone(PT)
    day = now_pt.date()
    for _ in range(_SEARCH_HORIZON_DAYS):
        if _is_slot_day(day, cfg, anchor):
            candidate = _slot_on(day, cfg)
            if candidate >= now_pt:
                return candidate
        day += timedelta(days=1)
    raise ValueError("no evaluation slot found within a year after `now`")  # pragma: no cover


def completed_slots(scheduler_state: SchedulerState) -> set[date]:
    """The set of slot dates already marked complete in ``scheduler_state``."""
    return set(scheduler_state.completed_slots)
