"""Scheduler (AS §1.5): slot computation, due jobs, the sweeper."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stockmanager.config.model import Config
from stockmanager.domain.docs import ProposalDoc
from stockmanager.scheduler.scheduler import DueJob, list_due_jobs
from stockmanager.scheduler.slots import completed_slots, latest_slot, next_slot
from stockmanager.scheduler.state import SchedulerState
from stockmanager.scheduler.sweeper import sweep
from stockmanager.store import FileStore
from tests.support.books import FakePidChecker, FixedClock
from tests.support.proposals import make_proposal, with_receipt

PT = ZoneInfo("America/Los_Angeles")

# -- slot computation ---------------------------------------------------------------------------------------


def test_latest_slot_daily_is_today_at_the_evaluation_time() -> None:
    cfg = Config(evaluation_frequency="daily", evaluation_time="09:00")
    now = datetime(2026, 10, 14, 10, 0, tzinfo=PT)  # Wednesday
    slot = latest_slot(now, cfg, None)
    assert slot == datetime(2026, 10, 14, 9, 0, tzinfo=PT)


def test_latest_slot_daily_before_time_falls_back_to_yesterday() -> None:
    cfg = Config(evaluation_frequency="daily", evaluation_time="09:00")
    now = datetime(2026, 10, 14, 8, 0, tzinfo=PT)
    slot = latest_slot(now, cfg, None)
    assert slot == datetime(2026, 10, 13, 9, 0, tzinfo=PT)


def test_latest_slot_weekly_picks_the_configured_weekday() -> None:
    cfg = Config(evaluation_frequency="weekly", evaluation_day="saturday", evaluation_time="09:00")
    now = datetime(2026, 10, 19, 12, 0, tzinfo=PT)  # Monday, after last Saturday's slot
    slot = latest_slot(now, cfg, None)
    assert slot == datetime(2026, 10, 17, 9, 0, tzinfo=PT)  # the preceding Saturday


def test_next_slot_weekly_is_the_upcoming_weekday() -> None:
    cfg = Config(evaluation_frequency="weekly", evaluation_day="saturday", evaluation_time="09:00")
    now = datetime(2026, 10, 19, 12, 0, tzinfo=PT)  # Monday
    slot = next_slot(now, cfg, None)
    assert slot == datetime(2026, 10, 24, 9, 0, tzinfo=PT)  # the next Saturday


def test_latest_and_next_slot_bracket_now() -> None:
    cfg = Config(evaluation_frequency="weekly", evaluation_day="saturday", evaluation_time="09:00")
    now = datetime(2026, 10, 20, 0, 0, tzinfo=PT)
    assert latest_slot(now, cfg, None) <= now <= next_slot(now, cfg, None)


def test_biweekly_parity_anchored_to_anchor_date() -> None:
    cfg = Config(evaluation_frequency="biweekly", evaluation_day="saturday", evaluation_time="09:00")
    anchor = date(2026, 10, 17)  # the first completed evaluation
    now = datetime(2026, 10, 24, 12, 0, tzinfo=PT)  # the following Saturday: odd parity, not a slot day
    slot = latest_slot(now, cfg, anchor)
    assert slot == datetime(2026, 10, 17, 9, 0, tzinfo=PT)  # falls back to the anchor itself
    now2 = datetime(2026, 10, 31, 12, 0, tzinfo=PT)  # two weeks after anchor: even parity, is a slot day
    assert latest_slot(now2, cfg, anchor) == datetime(2026, 10, 31, 9, 0, tzinfo=PT)


def test_dst_boundary_keeps_wall_clock_time_stable() -> None:
    # 2026-11-01 is the Sunday DST ends in the US; evaluation_time stays 09:00 PT on both sides.
    cfg = Config(evaluation_frequency="daily", evaluation_time="09:00")
    before = latest_slot(datetime(2026, 10, 31, 12, 0, tzinfo=PT), cfg, None)
    after = latest_slot(datetime(2026, 11, 2, 12, 0, tzinfo=PT), cfg, None)
    assert before.time().isoformat() == "09:00:00"
    assert after.time().isoformat() == "09:00:00"


def test_completed_slots_reads_scheduler_state() -> None:
    state = SchedulerState(completed_slots=(date(2026, 10, 17), date(2026, 10, 24)))
    assert completed_slots(state) == {date(2026, 10, 17), date(2026, 10, 24)}


# -- scheduler state persistence ---------------------------------------------------------------------------


def test_scheduler_state_round_trips_and_caps_completed_slots(tmp_path: Path) -> None:
    from stockmanager.scheduler import state as state_mod

    s = SchedulerState()
    for n in range(1, 11):
        s = s.with_completed(date(2026, 1, n))
    state_mod.save(tmp_path, s)
    loaded = state_mod.load(tmp_path)
    assert len(loaded.completed_slots) == 8
    assert loaded.anchor_date == date(2026, 1, 1)


def test_scheduler_state_load_missing_file_is_empty(tmp_path: Path) -> None:
    from stockmanager.scheduler import state as state_mod

    assert state_mod.load(tmp_path) == SchedulerState()


# -- due jobs -------------------------------------------------------------------------------------------------


def test_evaluation_job_due_when_live_and_slot_passed() -> None:
    cfg = Config(evaluation_frequency="daily", evaluation_time="09:00")
    now = datetime(2026, 10, 14, 9, 5, tzinfo=PT)
    jobs = list_due_jobs(now, cfg, "LIVE", [], SchedulerState())
    assert DueJob("evaluation", jobs[0].slot, None) in jobs


def test_evaluation_job_not_due_when_slot_already_completed() -> None:
    cfg = Config(evaluation_frequency="daily", evaluation_time="09:00")
    now = datetime(2026, 10, 14, 9, 5, tzinfo=PT)
    state = SchedulerState(completed_slots=(date(2026, 10, 14),))
    jobs = list_due_jobs(now, cfg, "LIVE", [], state)
    assert jobs == []


def test_evaluation_job_runs_while_paused_too() -> None:
    cfg = Config(evaluation_frequency="daily", evaluation_time="09:00")
    now = datetime(2026, 10, 14, 9, 5, tzinfo=PT)
    jobs = list_due_jobs(now, cfg, "PAUSED", [], SchedulerState())
    assert any(j.kind == "evaluation" for j in jobs)


def test_execution_job_due_inside_the_window_only_when_live() -> None:
    cfg = Config()
    execute_at = datetime(2026, 10, 19, 6, 30, tzinfo=PT)
    p = make_proposal(state="APPROVED", execute_at=execute_at)
    too_early = execute_at + timedelta(minutes=1)
    jobs = list_due_jobs(too_early, cfg, "LIVE", [p], SchedulerState())
    assert not any(j.kind == "execution" for j in jobs)  # before execute_at + exec_delay (5 min)
    inside = execute_at + timedelta(minutes=5)
    jobs = list_due_jobs(inside, cfg, "LIVE", [p], SchedulerState())
    assert DueJob("execution", None, p.proposal_id) in jobs
    assert not any(j.kind == "execution" for j in list_due_jobs(inside, cfg, "PAUSED", [p], SchedulerState()))


def test_execution_job_not_due_past_expiry() -> None:
    cfg = Config()
    execute_at = datetime(2026, 10, 19, 6, 30, tzinfo=PT)
    p = make_proposal(state="APPROVED", execute_at=execute_at)
    past = p.expires_at + timedelta(minutes=1)
    assert not any(j.kind == "execution" for j in list_due_jobs(past, cfg, "LIVE", [p], SchedulerState()))


def test_execution_jobs_sort_before_evaluation() -> None:
    cfg = Config(evaluation_frequency="daily", evaluation_time="09:00")
    execute_at = datetime(2026, 10, 14, 6, 30, tzinfo=PT)
    p = make_proposal(state="APPROVED", execute_at=execute_at)
    now = execute_at + timedelta(minutes=10)
    jobs = list_due_jobs(now, cfg, "LIVE", [p], SchedulerState())
    assert [j.kind for j in jobs] == ["execution", "evaluation"]


def test_opening_job_exists_only_when_approved() -> None:
    cfg = Config()
    now = datetime(2026, 10, 14, 9, 0, tzinfo=PT)
    assert [j.kind for j in list_due_jobs(now, cfg, "APPROVED", [], SchedulerState())] == ["opening"]
    assert list_due_jobs(now, cfg, "STAGED", [], SchedulerState()) == []
    assert list_due_jobs(now, cfg, "CLOSED", [], SchedulerState()) == []


# -- sweeper --------------------------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> FileStore:
    clock = FixedClock(datetime(2026, 10, 19, 9, 0, tzinfo=PT))
    s = FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)
    return s


def _seed_portfolio(store: FileStore, status: str) -> None:
    from stockmanager.domain.docs import PortfolioDoc

    store.save_portfolio(PortfolioDoc(start_cash=Decimal(10000), cash=Decimal(10000), status=status))  # type: ignore[arg-type]


def _write_proposal(store: FileStore, doc: ProposalDoc) -> None:
    store.save_proposal(doc.proposal_id, doc)


def test_sweep_cancels_non_terminal_proposals_when_paused(store: FileStore) -> None:
    _seed_portfolio(store, "PAUSED")
    p = make_proposal(state="PROPOSED", created_at=store.clock())
    _write_proposal(store, p)
    sweep(store.clock(), store, store.clock)
    data = json.loads(store.proposal_path(p.proposal_id).read_text())
    assert data["state"] == "CANCELLED_PAUSED"


def test_sweep_cancels_when_closed(store: FileStore) -> None:
    _seed_portfolio(store, "CLOSED")
    p = make_proposal(state="VETO_WINDOW", created_at=store.clock())
    _write_proposal(store, p)
    sweep(store.clock(), store, store.clock)
    data = json.loads(store.proposal_path(p.proposal_id).read_text())
    assert data["state"] == "CANCELLED_CLOSED"


def test_sweep_opens_veto_window_for_proposed(store: FileStore) -> None:
    _seed_portfolio(store, "LIVE")
    p = make_proposal(
        state="PROPOSED", created_at=store.clock(), veto_window_hours=4, execute_at=store.clock() + timedelta(hours=8)
    )
    _write_proposal(store, with_receipt(p, store.clock()))
    sweep(store.clock(), store, store.clock)
    data = json.loads(store.proposal_path(p.proposal_id).read_text())
    assert data["state"] == "VETO_WINDOW"
    assert data["veto_deadline"] is not None


def test_sweep_auto_approves_at_deadline(store: FileStore) -> None:
    _seed_portfolio(store, "LIVE")
    now = store.clock()
    p = make_proposal(state="VETO_WINDOW", created_at=now - timedelta(hours=5), execute_at=now + timedelta(hours=4))
    p = p.model_copy(update={"veto_deadline": now - timedelta(minutes=1)})
    _write_proposal(store, p)
    sweep(now, store, store.clock)
    data = json.loads(store.proposal_path(p.proposal_id).read_text())
    assert data["state"] == "APPROVED"


def test_sweep_expires_past_expires_at(store: FileStore) -> None:
    _seed_portfolio(store, "LIVE")
    now = store.clock()
    p = make_proposal(state="APPROVED", created_at=now - timedelta(days=2), execute_at=now - timedelta(hours=2))
    _write_proposal(store, p)
    sweep(now, store, store.clock)
    data = json.loads(store.proposal_path(p.proposal_id).read_text())
    assert data["state"] == "EXPIRED"


def test_sweep_is_idempotent(store: FileStore) -> None:
    _seed_portfolio(store, "PAUSED")
    p = make_proposal(state="PROPOSED", created_at=store.clock())
    _write_proposal(store, p)
    sweep(store.clock(), store, store.clock)
    first = store.proposal_path(p.proposal_id).read_text()
    sweep(store.clock(), store, store.clock)
    second = store.proposal_path(p.proposal_id).read_text()
    assert first == second


def test_sweep_deadline_too_short_expires_immediately(store: FileStore) -> None:
    # auto mode, created close enough to execute_at that the delivery deadline would pass execute_at (TS D10).
    _seed_portfolio(store, "LIVE")
    now = store.clock()
    p = make_proposal(
        state="PROPOSED", created_at=now, veto_window_hours=4, execute_at=now + timedelta(hours=1), approval_mode="auto"
    )
    _write_proposal(store, with_receipt(p, now))
    sweep(now, store, store.clock)
    data = json.loads(store.proposal_path(p.proposal_id).read_text())
    assert data["state"] == "EXPIRED"
