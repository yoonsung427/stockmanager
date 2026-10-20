"""Watchdog, heartbeat, lazy sweeper lock, alert dedup, outbox time budget, lock-before-app (AS §1.5, §9.2)."""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pytest

from stockmanager import cli
from stockmanager.config.model import Config
from stockmanager.domain.docs import PortfolioDoc
from stockmanager.execution.engine import RunResult
from stockmanager.notify import templates
from stockmanager.notify.deliver import Receipt, Rendered, deliver_pending
from stockmanager.notify.outbox import Alert, ChannelState, enqueue, pending_dedupe_keys, prepare
from stockmanager.scheduler import state as scheduler_state_mod
from stockmanager.scheduler.sweeper import sweep
from stockmanager.scheduler.tick import TICK_LOCK_NAME, TickTimeout, Watchdog, run_locked
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


class Ticker:
    """A fake monotonic clock that advances by ``step`` on every read."""

    def __init__(self, step: float = 0.0) -> None:
        self.t = 0.0
        self.step = step

    def __call__(self) -> float:
        value = self.t
        self.t += self.step
        return value


# -- watchdog ---------------------------------------------------------------------------------------------------


def test_watchdog_raises_only_after_max_runtime() -> None:
    mono = Ticker()
    dog = Watchdog(100.0, mono)
    mono.t = 100.0
    dog.check("a")  # exactly at the limit is still fine
    mono.t = 100.5
    with pytest.raises(TickTimeout, match="stage 'b'"):
        dog.check("b")


class AdvancingEngine:
    """Execution stand-in whose job 'takes' ``seconds`` on the fake monotonic clock."""

    def __init__(self, mono: Ticker, seconds: float) -> None:
        self.mono, self.seconds = mono, seconds
        self.ran: list[str] = []
        self.last_tick_seen: datetime | None = None
        self.root: Path | None = None

    def run_proposal(self, proposal_id: str) -> RunResult:
        if self.root is not None:
            self.last_tick_seen = scheduler_state_mod.load(self.root).last_tick
        self.ran.append(proposal_id)
        self.mono.t += self.seconds
        return RunResult("completed")

    def run_opening(self) -> RunResult:
        return RunResult("completed")


class CountingEvaluation:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, slot: Any, cfg: Any) -> RunResult:
        self.calls += 1
        return RunResult("completed")


def _approved_with_due_slot(store: FileStore, clock: FixedClock) -> str:
    seed(store, "LIVE")
    execute_at = clock.now - timedelta(minutes=10)
    p = make_proposal(state="APPROVED", created_at=execute_at - timedelta(hours=4), execute_at=execute_at)
    store.save_proposal(p.proposal_id, p)
    return p.proposal_id


def test_tick_stops_at_stage_boundary_after_overrun(store: FileStore, clock: FixedClock) -> None:
    pid = _approved_with_due_slot(store, clock)
    mono = Ticker()
    engine = AdvancingEngine(mono, seconds=1300.0)  # execution job overruns the 20 min watchdog
    evaluation = CountingEvaluation()
    cfg = Config(evaluation_frequency="daily", evaluation_time="09:00")
    code = run_locked(
        clock, store, cfg, execution=engine, evaluation=evaluation, monotonic=mono  # type: ignore[arg-type]
    )
    assert code == 1
    assert engine.ran == [pid]  # the job in flight was allowed to finish (never interrupted mid-commit)
    assert evaluation.calls == 0  # ...but the next stage never started
    assert scheduler_state_mod.load(store.root).completed_slots == ()  # retried on the next tick


def test_tick_within_budget_is_unaffected(store: FileStore, clock: FixedClock) -> None:
    pid = _approved_with_due_slot(store, clock)
    mono = Ticker()
    engine = AdvancingEngine(mono, seconds=60.0)
    cfg = Config(evaluation_frequency="daily", evaluation_time="09:00")
    assert run_locked(clock, store, cfg, execution=engine, monotonic=mono) == 0  # type: ignore[arg-type]
    assert engine.ran == [pid]


def test_tick_with_zero_runtime_stops_before_any_work(store: FileStore, clock: FixedClock) -> None:
    pid = _approved_with_due_slot(store, clock)
    engine = AdvancingEngine(Ticker(), seconds=0)
    code = run_locked(clock, store, Config(), execution=engine, max_runtime=-1.0)  # type: ignore[arg-type]
    assert code == 1
    assert engine.ran == []
    assert json.loads(store.proposal_path(pid).read_text())["state"] == "APPROVED"


# -- last_tick at tick start ------------------------------------------------------------------------------------


def test_last_tick_is_written_before_jobs_run(store: FileStore, clock: FixedClock) -> None:
    _approved_with_due_slot(store, clock)
    engine = AdvancingEngine(Ticker(), seconds=0)
    engine.root = store.root
    assert scheduler_state_mod.load(store.root).last_tick is None
    assert run_locked(clock, store, Config(), execution=engine) == 0  # type: ignore[arg-type]
    assert engine.last_tick_seen == clock.now  # visible while the job was still running


def test_last_tick_written_even_when_tick_fails(store: FileStore, clock: FixedClock) -> None:
    run_locked(clock, store, Config(), max_runtime=-1.0)
    assert scheduler_state_mod.load(store.root).last_tick == clock.now


# -- sweeper locks only when there is work ----------------------------------------------------------------------


def _count_transactions(store: FileStore, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    jobs: list[str] = []
    real = store.transaction

    def counting(job: str, **kw: Any) -> Any:
        jobs.append(job)
        return real(job, **kw)

    monkeypatch.setattr(store, "transaction", counting)
    return jobs


def test_sweep_takes_no_lock_when_nothing_to_do(store: FileStore, clock: FixedClock, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(store, "LIVE")
    p = make_proposal(state="APPROVED", created_at=clock.now - timedelta(hours=1), execute_at=clock.now + timedelta(hours=1))
    store.save_proposal(p.proposal_id, p)
    jobs = _count_transactions(store, monkeypatch)
    assert sweep(clock.now, store, clock) == []
    assert jobs == []
    assert not (store.root / "locks").exists() or not list((store.root / "locks").glob("portfolio*"))


def test_sweep_with_no_proposals_takes_no_lock(store: FileStore, clock: FixedClock, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(store, "LIVE")
    jobs = _count_transactions(store, monkeypatch)
    assert sweep(clock.now, store, clock) == []
    assert jobs == []


def test_sweep_takes_the_lock_once_when_there_is_work(
    store: FileStore, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(store, "PAUSED")
    p = make_proposal(state="PROPOSED", created_at=clock.now)
    store.save_proposal(p.proposal_id, p)
    jobs = _count_transactions(store, monkeypatch)
    written = sweep(clock.now, store, clock)
    assert jobs == ["sweep"]
    assert [t.to for t in written] == ["CANCELLED_PAUSED"]
    assert sweep(clock.now, store, clock) == []  # idempotent, and now lock-free again
    assert jobs == ["sweep"]


# -- alert dedup ------------------------------------------------------------------------------------------------


def _pending_files(store: FileStore) -> list[Path]:
    return sorted((store.root / "alerts" / "pending").glob("*.json"))


def test_enqueue_skips_alert_with_pending_dedupe_key(store: FileStore, clock: FixedClock) -> None:
    assert enqueue(templates.bot_offline(), store) is True
    clock.advance(seconds=1)
    assert enqueue(templates.bot_offline(), store) is False
    assert len(_pending_files(store)) == 1
    assert pending_dedupe_keys(store.root) == {"bot_offline"}


def test_enqueue_allows_different_keys_and_keyless_alerts(store: FileStore, clock: FixedClock) -> None:
    assert enqueue(templates.bot_offline(), store)
    clock.advance(seconds=1)
    assert enqueue(templates.scheduler_stalled(), store)
    for _ in range(2):
        clock.advance(seconds=1)
        assert enqueue(Alert("k", "info", "t", "b", {}, [], False, None, {}), store)
    assert len(_pending_files(store)) == 4


def test_dedupe_key_is_free_again_once_delivered(store: FileStore, clock: FixedClock) -> None:
    enqueue(templates.bot_offline(), store)
    ch = _Channel("mainchat", store)
    deliver_pending(store, [ch, _Channel("discord", store)], clock)
    assert _pending_files(store) == []
    clock.advance(seconds=1)
    assert enqueue(templates.bot_offline(), store) is True


def test_stage_alerts_drops_duplicates_of_pending_and_of_the_same_transaction(
    store: FileStore, clock: FixedClock
) -> None:
    seed(store)
    enqueue(templates.bot_offline(), store)
    staged = [_prep(a, clock) for a in (templates.bot_offline(), templates.scheduler_stalled(), templates.scheduler_stalled())]
    with store.transaction("t") as txn:
        txn.stage_alerts({f"{a.alert_id}.json": a.to_json() for a in staged})
    keys = [json.loads(p.read_text())["dedupe_key"] for p in _pending_files(store)]
    assert sorted(keys) == ["bot_offline", "scheduler_stalled"]


def _prep(alert: Alert, clock: FixedClock) -> Alert:
    clock.advance(seconds=1)
    return prepare(alert, clock.now)


# -- outbox time budget -----------------------------------------------------------------------------------------


class _Channel:
    def __init__(self, name: Literal["discord", "mainchat"], store: FileStore, mono: Ticker | None = None, cost: float = 0.0) -> None:
        self.name = name
        self.sent: list[str] = []
        self._mono, self._cost = mono, cost

    def send(self, rendered: Rendered, *, nonce: str) -> Receipt:
        self.sent.append(nonce)
        if self._mono is not None:
            self._mono.t += self._cost
        return Receipt(channel=self.name, external_id=nonce, sent_at=datetime(2026, 10, 14, tzinfo=PT))


def _queue(store: FileStore, clock: FixedClock, n: int) -> list[str]:
    ids = []
    for i in range(n):
        clock.advance(seconds=1)
        a = templates.proposal_expired(f"P-{i}", "x")
        a.channels = {"mainchat": ChannelState()}
        enqueue(a, store)
        ids.append(a.alert_id)
    return ids


def test_deliver_stops_when_time_budget_is_spent(store: FileStore, clock: FixedClock) -> None:
    ids = _queue(store, clock, 5)
    mono = Ticker()
    ch = _Channel("mainchat", store, mono, cost=8.0)
    delivered = deliver_pending(store, [ch], clock, time_budget=20.0, monotonic=mono)
    assert delivered == ids[:3]  # sends at t=0, 8, 16 start in budget; the one at t=24 does not
    assert len(_pending_files(store)) == 2  # the rest wait, in order, for the next drain
    assert len(list((store.root / "alerts" / "sent").glob("*.json"))) == 3


def test_deliver_time_budget_does_not_cut_a_fast_drain(store: FileStore, clock: FixedClock) -> None:
    ids = _queue(store, clock, 5)
    mono = Ticker()
    assert deliver_pending(store, [_Channel("mainchat", store, mono, 1.0)], clock, time_budget=20.0, monotonic=mono) == ids


def test_deliver_zero_time_budget_sends_nothing(store: FileStore, clock: FixedClock) -> None:
    _queue(store, clock, 2)
    ch = _Channel("mainchat", store)
    assert deliver_pending(store, [ch], clock, time_budget=0.0, monotonic=Ticker()) == []
    assert ch.sent == []
    assert len(_pending_files(store)) == 2


def test_deliver_without_time_budget_keeps_count_budget(store: FileStore, clock: FixedClock) -> None:
    ids = _queue(store, clock, 5)
    assert deliver_pending(store, [_Channel("mainchat", store)], clock, budget=2) == ids[:2]


# -- tick takes the lock before building the app ----------------------------------------------------------------


def test_cmd_tick_does_not_build_app_when_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from stockmanager.app import init_workspace

    ws = tmp_path / "ws"
    init_workspace(ws)

    def boom(*a: object, **k: object) -> object:
        raise AssertionError("create_app must not run while another tick holds the lock")

    monkeypatch.setattr(cli, "create_app", boom)
    lock_path = ws / "state" / TICK_LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert cli.main(["tick", "--workspace", str(ws)]) == 0
    finally:
        os.close(fd)
    assert "skipped" in capsys.readouterr().out


def test_cmd_tick_builds_app_under_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from stockmanager.app import create_app, init_workspace

    ws = tmp_path / "ws"
    init_workspace(ws)
    held: list[bool] = []

    def spying(*a: Any, **k: Any) -> Any:
        fd = os.open(ws / "state" / TICK_LOCK_NAME, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(False)
        except OSError:
            held.append(True)  # we could not take it, because the CLI already holds it
        finally:
            os.close(fd)
        return create_app(*a, **k)

    monkeypatch.setattr(cli, "create_app", spying)
    assert cli.main(["tick", "--workspace", str(ws)]) == 0
    assert held == [True]
