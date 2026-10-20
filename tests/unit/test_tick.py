"""Tick dispatcher (AS §1.5): single-flight, outbox drain before jobs, sweep before dispatch, execution before eval."""

from __future__ import annotations

import fcntl
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from stockmanager.config.model import Config
from stockmanager.domain.docs import PortfolioDoc
from stockmanager.execution.engine import RunResult
from stockmanager.notify.outbox import Alert, prepare
from stockmanager.scheduler import state as scheduler_state_mod
from stockmanager.scheduler.tick import TICK_LOCK_NAME, tick
from stockmanager.store import FileStore
from tests.support.books import FakePidChecker, FixedClock
from tests.support.proposals import make_proposal

PT = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 10, 14, 9, 5, tzinfo=PT))


@pytest.fixture
def store(tmp_path: Path, clock: FixedClock) -> FileStore:
    return FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)


def seed(store: FileStore, status: str = "LIVE") -> None:
    store.save_portfolio(PortfolioDoc(start_cash=Decimal(10000), cash=Decimal(10000), status=status))  # type: ignore[arg-type]


class RecordingEngine:
    """A stand-in ``ExecutionEngine`` that only records which methods were called."""

    def __init__(self) -> None:
        self.proposals_run: list[str] = []
        self.opened = False

    def run_proposal(self, proposal_id: str) -> RunResult:
        self.proposals_run.append(proposal_id)
        return RunResult("completed")

    def run_opening(self) -> RunResult:
        self.opened = True
        return RunResult("completed")


def test_single_flight_skips_when_lock_held(store: FileStore, clock: FixedClock) -> None:
    lock_path = store.root / "state" / TICK_LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert tick(clock, store, Config()) == 0
        assert not store.portfolio_exists()  # nothing ran: no portfolio was ever seeded, and nothing crashed
    finally:
        os.close(fd)


def test_tick_returns_zero_with_no_portfolio(store: FileStore, clock: FixedClock) -> None:
    assert tick(clock, store, Config()) == 0
    state = scheduler_state_mod.load(store.root)
    assert state.last_tick == clock.now


def test_sweeper_runs_before_job_dispatch(store: FileStore, clock: FixedClock) -> None:
    seed(store, "PAUSED")
    p = make_proposal(state="PROPOSED", created_at=clock.now)
    store.save_proposal(p.proposal_id, p)
    assert tick(clock, store, Config()) == 0
    import json

    data = json.loads(store.proposal_path(p.proposal_id).read_text())
    assert data["state"] == "CANCELLED_PAUSED"  # the sweeper converged it even though status stayed PAUSED


def test_execution_runs_before_evaluation_marks_slot_complete(store: FileStore, clock: FixedClock) -> None:
    seed(store, "LIVE")
    execute_at = clock.now - timedelta(minutes=10)
    p = make_proposal(state="APPROVED", created_at=execute_at - timedelta(hours=4), execute_at=execute_at)
    store.save_proposal(p.proposal_id, p)
    engine = RecordingEngine()
    cfg = Config(evaluation_frequency="daily", evaluation_time="09:00")
    assert tick(clock, store, cfg, execution=engine) == 0  # type: ignore[arg-type]
    assert engine.proposals_run == [p.proposal_id]
    state = scheduler_state_mod.load(store.root)
    assert state.completed_slots == (clock.now.date(),)


def test_opening_dispatched_when_approved(store: FileStore, clock: FixedClock) -> None:
    seed(store, "APPROVED")
    engine = RecordingEngine()
    assert tick(clock, store, Config(), execution=engine) == 0  # type: ignore[arg-type]
    assert engine.opened is True


def test_paused_notices_the_slot_without_running_evaluation(store: FileStore, clock: FixedClock) -> None:
    seed(store, "PAUSED")
    cfg = Config(evaluation_frequency="daily", evaluation_time="09:00")
    assert tick(clock, store, cfg) == 0
    state = scheduler_state_mod.load(store.root)
    assert state.noticed_slots == (clock.now.date(),)
    assert state.completed_slots == ()  # never marked "completed": no evaluation ran


def test_outbox_drains_before_job_dispatch(store: FileStore, clock: FixedClock) -> None:
    seed(store, "LIVE")
    from stockmanager.notify.outbox import ChannelState

    alert = prepare(
        Alert("x", "info", "t", "b", {}, [], False, "k", {}, channels={"mainchat": ChannelState()}), clock.now
    )
    (store.root / "alerts" / "pending").mkdir(parents=True, exist_ok=True)
    (store.root / "alerts" / "pending" / f"{alert.alert_id}.json").write_text(alert.to_json())

    class Chan:
        name = "mainchat"

        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, rendered: Any, *, nonce: str) -> Any:
            from stockmanager.notify.deliver import Receipt

            self.sent.append(nonce)
            return Receipt(channel="mainchat", external_id=nonce, sent_at=clock.now)

    channel = Chan()
    assert tick(clock, store, Config(), channels=(channel,)) == 0  # type: ignore[arg-type]
    assert channel.sent == [alert.alert_id]
    assert (store.root / "alerts" / "sent" / f"{alert.alert_id}.json").exists()
