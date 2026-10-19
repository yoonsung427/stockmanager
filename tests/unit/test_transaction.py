"""Transaction: gate, reconcile, fixed commit order, no writes on failure (AS §8.5)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from stockmanager.store import FileStore
from stockmanager.store.lock import LockBusy
from stockmanager.store.reconcile import format_log_line
from stockmanager.store.transaction import GateError, ReconcileHalt, RunNote, TransactionError
from tests.support.books import FakePidChecker, FixedClock, make_portfolio, make_trade


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def store(tmp_path: Path, clock: FixedClock) -> FileStore:
    return FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)


def seed(store: FileStore, n_trades: int = 1, *, log: bool = True, status: str = "LIVE") -> None:
    trades = [make_trade(i) for i in range(1, n_trades + 1)]
    store.save_portfolio(make_portfolio(trades, status=status))
    if log and trades:
        store.append_trades_log([format_log_line(t) for t in trades])


def tree(store: FileStore) -> dict[str, bytes]:
    """Every file under the workspace except the (persistent, empty) lock guard."""
    return {
        str(p.relative_to(store.root)): p.read_bytes()
        for p in sorted(store.root.rglob("*"))
        if p.is_file() and p.name != ".portfolio.lock.guard"
    }


def recorder(order: list[str], label: str, real: Callable[..., Any]) -> Callable[..., None]:
    def call(*args: Any, **kwargs: Any) -> None:
        order.append(label)
        real(*args, **kwargs)

    return call


def stage_everything(tx: Any) -> None:
    new = tx.state.model_copy(update={"trades": [*tx.state.trades, make_trade(2)]})
    tx.stage_portfolio(new)
    tx.stage_log([format_log_line(make_trade(2))])
    tx.stage_proposals({"P-1": {"state": "EXECUTED"}})
    tx.stage_extras([{"date": "2026-10-12"}], {"eval-2026-10-12.json": "{}\n"}, [{"kind": "log_line"}])
    tx.stage_run_note(RunNote("n.md", "note"))
    tx.stage_dashboard({"portfolio.json": "{}\n"})
    tx.stage_alerts({"a.json": "{}\n"})


def test_commit_writes_everything_and_releases_lock(store: FileStore, clock: FixedClock) -> None:
    seed(store)
    with store.transaction("execution") as tx:
        assert store.lock.is_held()
        stage_everything(tx)
        assert len(store.load_portfolio().trades) == 1  # nothing written until exit
    assert not store.lock.is_held()
    doc = store.load_portfolio()
    assert len(doc.trades) == 2 and doc.rev == 1 and doc.updated_at == clock.now
    assert len(store.trades_log_path.read_text().splitlines()) == 2
    r = store.root
    for rel in (
        "proposals/P-1.json",
        "nav_history.jsonl",
        "evaluations/eval-2026-10-12.json",
        "acks.jsonl",
        "run-notes/n.md",
        "dashboard/portfolio.json",
        "alerts/pending/a.json",
    ):
        assert (r / rel).exists(), rel


def test_commit_order_is_fixed(store: FileStore, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(store)
    order: list[str] = []
    for method, label in [
        ("save_portfolio", "portfolio"),
        ("append_trades_log", "log"),
        ("save_proposal", "proposal"),
        ("save_nav_history", "nav"),
    ]:
        real = getattr(store, method)
        monkeypatch.setattr(store, method, recorder(order, label, real))
    real_extras = store.save_extras

    def extras(**kw: Any) -> None:
        for key in ("evaluations", "acks", "run_notes", "dashboard", "alerts"):
            if kw.get(key):
                order.append(key)
        real_extras(**kw)

    monkeypatch.setattr(store, "save_extras", extras)
    with store.transaction("execution") as tx:
        stage_everything(tx)
    assert order == ["portfolio", "log", "proposal", "nav", "evaluations", "acks", "run_notes", "dashboard", "alerts"]


def test_exception_in_body_writes_nothing_and_releases(store: FileStore) -> None:
    seed(store)
    before = tree(store)
    with pytest.raises(RuntimeError, match="boom"), store.transaction("execution") as tx:
        stage_everything(tx)
        raise RuntimeError("boom")
    assert tree(store) == before
    assert not store.lock.is_held()


@pytest.mark.parametrize("fail_at", ["save_portfolio", "append_trades_log", "save_proposal", "save_nav_history"])
def test_failure_mid_commit_skips_remaining_writes_and_releases(
    store: FileStore, monkeypatch: pytest.MonkeyPatch, fail_at: str
) -> None:
    seed(store)

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, fail_at, boom)
    with pytest.raises(OSError, match="disk full"), store.transaction("execution") as tx:
        stage_everything(tx)
    assert not store.lock.is_held()
    r = store.root
    for later in ("run-notes/n.md", "dashboard/portfolio.json", "alerts/pending/a.json"):
        assert not (r / later).exists()
    if fail_at == "save_portfolio":
        assert not (r / "proposals").exists() and len(store.load_portfolio().trades) == 1
        assert len(store.trades_log_path.read_text().splitlines()) == 1


def test_gate_failure_aborts_without_writing(store: FileStore) -> None:
    seed(store, status="PAUSED")
    before = tree(store)
    with pytest.raises(GateError, match="LIVE"), store.transaction("execution") as tx:
        tx.require(status="LIVE")
        stage_everything(tx)
    assert tree(store) == before
    assert not store.lock.is_held()


def test_gate_accepts_expected_status_or_any_of_several(store: FileStore) -> None:
    seed(store)
    with store.transaction("execution") as tx:
        tx.require(status="LIVE")
        tx.require(status=("LIVE", "PAUSED"))


def test_gate_with_no_portfolio_fails(store: FileStore) -> None:
    with pytest.raises(GateError, match="missing"), store.transaction("stage") as tx:
        tx.require(status="STAGED")


def test_lock_busy_does_not_enter_or_touch_lock(store: FileStore) -> None:
    seed(store)
    held = store.lock.acquire("other", 1, 0)
    with pytest.raises(LockBusy), store.transaction("execution", max_attempts=2, sleep_seconds=0):
        pytest.fail("body must not run")
    assert store.lock.is_held()  # the other holder's lock is untouched
    store.lock.release(held.token)


def test_enter_cleans_stale_tmp_files_under_lock(store: FileStore) -> None:
    seed(store)
    (store.root / "portfolio.json.tmp").write_text("half-written")
    with store.transaction("execution"):
        assert not (store.root / "portfolio.json.tmp").exists()
    assert len(store.load_portfolio().trades) == 1


def test_stage_portfolio_bumps_rev_each_commit(store: FileStore) -> None:
    seed(store)
    for expected in (1, 2):
        with store.transaction("evaluation") as tx:
            tx.stage_portfolio(tx.state)
        assert store.load_portfolio().rev == expected


def test_stage_portfolio_rejects_rewriting_history(store: FileStore) -> None:
    seed(store, 2)
    with pytest.raises(TransactionError, match="append-only"), store.transaction("x") as tx:
        tx.stage_portfolio(tx.state.model_copy(update={"trades": tx.state.trades[:1]}))
    assert len(store.load_portfolio().trades) == 2


def test_stage_portfolio_creates_first_document(store: FileStore) -> None:
    with store.transaction("stage") as tx:
        tx.stage_portfolio(make_portfolio(status="STAGED"))
    assert store.load_portfolio().rev == 1


def test_only_staged_files_are_written_for_evaluation_without_book_changes(store: FileStore) -> None:
    seed(store)
    before = (store.portfolio_path.read_bytes(), store.trades_log_path.read_bytes())
    with store.transaction("evaluation") as tx:
        tx.stage_extras(eval_docs={"eval-2026-10-12.json": "{}\n"})
    assert (store.portfolio_path.read_bytes(), store.trades_log_path.read_bytes()) == before


# -- reconcile inside the transaction -----------------------------------------------------------------------------


def test_reconcile_ok(store: FileStore) -> None:
    seed(store, 2)
    with store.transaction("execution") as tx:
        rebuilt = tx.reconcile()
        assert rebuilt.positions == tx.state.positions
        assert tx.repaired_log_ids == []


def test_reconcile_repairs_missing_log_suffix_without_refilling(store: FileStore) -> None:
    seed(store, 3, log=False)
    store.append_trades_log([format_log_line(make_trade(1))])  # crash after CP2: books ahead of the log
    with store.transaction("execution") as tx:
        tx.reconcile()
        assert tx.repaired_log_ids == [make_trade(2).trade_id, make_trade(3).trade_id]
    assert store.trades_log_path.read_text().splitlines() == [format_log_line(make_trade(i)) for i in (1, 2, 3)]
    assert len(store.load_portfolio().trades) == 3


def test_reconcile_halts_on_torn_log_line(store: FileStore) -> None:
    seed(store, 2, log=False)
    store.root.mkdir(parents=True, exist_ok=True)
    store.trades_log_path.write_bytes(format_log_line(make_trade(1)).encode() + b"\n" + b"P-OPEN-2026-10-12-bu")
    before = tree(store)
    with pytest.raises(ReconcileHalt, match="torn"), store.transaction("execution") as tx:
        tx.reconcile()
    assert tree(store) == before
    assert not store.lock.is_held()


def test_reconcile_halts_on_position_drift(store: FileStore) -> None:
    seed(store)
    doc = store.load_portfolio()
    store.save_portfolio(doc.model_copy(update={"cash": doc.cash + 5}))
    with pytest.raises(ReconcileHalt, match="cash"), store.transaction("execution") as tx:
        tx.reconcile()


def test_committed_json_is_valid(store: FileStore) -> None:
    seed(store)
    with store.transaction("execution") as tx:
        tx.stage_proposals({"P-1": {"state": "EXECUTED"}})
    assert json.loads((store.root / "proposals/P-1.json").read_text()) == {"state": "EXECUTED"}
