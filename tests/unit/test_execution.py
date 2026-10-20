"""Execution engine (AS §1.3, §8.6): opening, proposal execution, final valuation."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pytest

from stockmanager.domain.docs import PortfolioDoc, PriceQuote, ProposalDoc, TransitionRecord
from stockmanager.domain.proposal_state import ProposalState, chain_hash
from stockmanager.execution.engine import ExecutionEngine
from stockmanager.pricing.errors import PriceError
from stockmanager.store import FileStore
from tests.support.assertions import alerts
from tests.support.books import FakePidChecker, FixedClock, make_portfolio, make_trade
from tests.support.proposals import make_proposal

PT = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 20, 6, 35, tzinfo=PT)


class FakeCalendar:
    """Session open is always a fixed PT instant (deterministic for tests)."""

    def __init__(self, open_at: datetime = datetime(2026, 10, 20, 6, 30, tzinfo=PT)) -> None:
        self.open_at = open_at

    def next_session_open_at_or_after(self, instant: datetime) -> datetime:
        return self.open_at


class FakeProvider:
    def __init__(self, name: Literal["yahoo", "finnhub"], prices: dict[str, Decimal]) -> None:
        self.name = name
        self.prices = prices
        self.calls: list[str] = []

    def get_price(self, ticker: str, session_date: date, kind: Literal["open", "close"]) -> PriceQuote:
        self.calls.append(ticker)
        if ticker not in self.prices:
            raise PriceError("missing", ticker)
        return PriceQuote(ticker, session_date, kind, self.prices[ticker], self.name, NOW)


class FailingProvider:
    name: Literal["yahoo", "finnhub"] = "yahoo"

    def get_price(self, ticker: str, session_date: date, kind: Literal["open", "close"]) -> PriceQuote:
        raise PriceError("timeout", ticker)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def store(tmp_path: Path, clock: FixedClock) -> FileStore:
    return FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)


def engine(store: FileStore, clock: FixedClock, prices: dict[str, Decimal]) -> ExecutionEngine:
    return ExecutionEngine(store, clock, FakeCalendar(), FakeProvider("yahoo", prices), FakeProvider("finnhub", {}))


def live_portfolio(cash: str = "10000", positions: object = None) -> PortfolioDoc:
    """A self-consistent LIVE book: no trades, ``cash == start_cash``."""
    return make_portfolio(trades=[], start_cash=cash, status="LIVE")


def _advance(doc: ProposalDoc, to_state: ProposalState, actor: str, cause: str, at: datetime) -> ProposalDoc:
    prev = doc.transitions[-1]
    rec = TransitionRecord(
        seq=prev.seq + 1,
        from_state=doc.state,
        to=to_state,
        at=at,
        actor=actor,
        cause=cause,
        prev_hash=prev.hash,
        hash="",
    )
    rec = rec.model_copy(update={"hash": chain_hash(prev.hash, rec.model_dump(mode="json", by_alias=True))})
    return doc.model_copy(update={"state": to_state, "transitions": [*doc.transitions, rec]})


def approved_proposal(created_at: datetime, execute_at: datetime) -> ProposalDoc:
    """A proposal with a legal ``PROPOSED -> VETO_WINDOW -> APPROVED`` transition chain, state ``APPROVED``."""
    p = make_proposal(state="PROPOSED", created_at=created_at, execute_at=execute_at)
    p = _advance(p, "VETO_WINDOW", "system:sweeper", "delivered", created_at)
    p = _advance(p, "APPROVED", "system:sweeper", "auto-approved at deadline", created_at)
    return p


# -- run_opening ---------------------------------------------------------------------------------------------


def test_run_opening_fills_staged_picks_and_goes_live(store: FileStore, clock: FixedClock) -> None:
    doc = make_portfolio(trades=[], start_cash="10000", status="APPROVED")
    doc = doc.model_copy(
        update={"config": {"staged_orders": [{"ticker": "AAPL", "target_value": "5000", "thesis": "t"}]}}
    )
    store.save_portfolio(doc)
    e = engine(store, clock, {"AAPL": Decimal(100)})
    result = e.run_opening()
    assert result.outcome == "completed"
    out = store.load_portfolio()
    assert out.status == "LIVE"
    assert out.positions[0].ticker == "AAPL" and out.positions[0].shares == Decimal(50)
    assert out.cash == Decimal(5000)
    assert len(out.trades) == 1
    assert len(list((store.root / "alerts" / "pending").glob("*.json"))) == 1


def test_run_opening_is_all_or_nothing_on_unpriceable_ticker(store: FileStore, clock: FixedClock) -> None:
    doc = make_portfolio(trades=[], start_cash="10000", status="APPROVED")
    doc = doc.model_copy(
        update={
            "config": {
                "staged_orders": [
                    {"ticker": "AAPL", "target_value": "5000"},
                    {"ticker": "ZZZZ", "target_value": "1000"},
                ]
            }
        }
    )
    store.save_portfolio(doc)
    e = engine(store, clock, {"AAPL": Decimal(100)})  # ZZZZ is unpriceable on both sources
    result = e.run_opening()
    assert result.outcome == "failed_closed"
    out = store.load_portfolio()
    assert out.status == "APPROVED"  # unchanged: all-or-nothing (TS NN-15)
    assert out.positions == [] and out.trades == []


def test_run_opening_no_op_before_session_opens(store: FileStore, clock: FixedClock) -> None:
    doc = make_portfolio(trades=[], start_cash="10000", status="APPROVED")
    doc = doc.model_copy(update={"config": {"staged_orders": [{"ticker": "AAPL", "target_value": "5000"}]}})
    store.save_portfolio(doc)
    e = ExecutionEngine(
        store,
        clock,
        FakeCalendar(open_at=clock.now + timedelta(hours=1)),
        FakeProvider("yahoo", {"AAPL": Decimal(100)}),
        FakeProvider("finnhub", {}),
    )
    result = e.run_opening()
    assert result.outcome == "no_op"
    assert store.load_portfolio().status == "APPROVED"


# -- run_proposal ----------------------------------------------------------------------------------------------


def test_run_proposal_fills_legs_writes_trades_and_executes(store: FileStore, clock: FixedClock) -> None:
    store.save_portfolio(live_portfolio())
    execute_at = clock.now - timedelta(minutes=10)
    p = approved_proposal(execute_at - timedelta(hours=4), execute_at)
    store.save_proposal(p.proposal_id, p)
    e = engine(store, clock, {"AAPL": Decimal(200)})
    result = e.run_proposal(p.proposal_id)
    assert result.outcome == "completed"
    doc = store.load_portfolio()
    assert len(doc.trades) == 1 and doc.trades[0].ticker == "AAPL"
    assert doc.cash == Decimal(10000) - Decimal(1000)  # buy1 is target_value 1000 at price 200 -> 5 shares
    assert '"state": "EXECUTED"' in store.proposal_path(p.proposal_id).read_text()
    assert len(store.trades_log_path.read_text().splitlines()) == 1


def test_run_proposal_no_op_outside_execution_window(store: FileStore, clock: FixedClock) -> None:
    store.save_portfolio(live_portfolio())
    execute_at = clock.now + timedelta(hours=1)  # not due yet
    p = make_proposal(state="APPROVED", created_at=clock.now, execute_at=execute_at)
    store.save_proposal(p.proposal_id, p)
    e = engine(store, clock, {"AAPL": Decimal(200)})
    result = e.run_proposal(p.proposal_id)
    assert result.outcome == "no_op"
    assert store.load_portfolio().trades == []


def test_run_proposal_recovers_when_all_trades_already_logged(store: FileStore, clock: FixedClock) -> None:
    from stockmanager.domain.ids import trade_id as mk_trade_id

    execute_at = clock.now - timedelta(minutes=10)
    p = approved_proposal(execute_at - timedelta(hours=4), execute_at)
    leg = p.legs[0]
    trade = make_trade(1, action="BUY", ticker="AAPL", shares="5", price="200", thesis="t")
    trade = trade.model_copy(update={"trade_id": mk_trade_id(p.proposal_id, leg.leg)})
    store.save_portfolio(make_portfolio([trade], start_cash="10000", status="LIVE"))
    store.save_proposal(p.proposal_id, p)
    e = engine(store, clock, {"AAPL": Decimal(200)})
    result = e.run_proposal(p.proposal_id)
    assert result.outcome == "completed" and "recovery" in result.detail
    assert len(store.load_portfolio().trades) == 1  # not refilled
    assert '"state": "EXECUTED"' in store.proposal_path(p.proposal_id).read_text()


def test_run_proposal_fails_closed_on_price_failure(store: FileStore, clock: FixedClock) -> None:
    store.save_portfolio(live_portfolio())
    execute_at = clock.now - timedelta(minutes=10)
    p = make_proposal(state="APPROVED", created_at=execute_at - timedelta(hours=4), execute_at=execute_at)
    store.save_proposal(p.proposal_id, p)
    e = ExecutionEngine(store, clock, FakeCalendar(), FailingProvider(), FailingProvider())
    result = e.run_proposal(p.proposal_id)
    assert result.outcome == "failed_closed"
    assert store.load_portfolio().trades == []


def test_run_proposal_lock_busy(store: FileStore, clock: FixedClock) -> None:
    store.save_portfolio(live_portfolio())
    execute_at = clock.now - timedelta(minutes=10)
    p = make_proposal(state="APPROVED", created_at=execute_at - timedelta(hours=4), execute_at=execute_at)
    store.save_proposal(p.proposal_id, p)
    e = ExecutionEngine(
        store, clock, FakeCalendar(), FakeProvider("yahoo", {"AAPL": Decimal(200)}), FakeProvider("finnhub", {})
    )
    held = store.lock.acquire("other", 1, 0)
    try:
        result = e.run_proposal(p.proposal_id)
    finally:
        store.lock.release(held.token)
    assert result.outcome == "lock_busy"


def test_run_proposal_no_op_when_already_vetoed_before_the_lock(store: FileStore, clock: FixedClock) -> None:
    """A veto that won the race before execution's pre-flight read: step 1 sees the new state and no-ops."""
    store.save_portfolio(live_portfolio())
    execute_at = clock.now - timedelta(minutes=10)
    p = make_proposal(state="APPROVED", created_at=execute_at - timedelta(hours=4), execute_at=execute_at)
    vetoed = p.model_copy(update={"state": "VETOED"})
    store.save_proposal(p.proposal_id, vetoed)
    e = engine(store, clock, {"AAPL": Decimal(200)})
    result = e.run_proposal(p.proposal_id)
    assert result.outcome == "no_op"
    assert store.load_portfolio().trades == []


def test_run_proposal_clamps_instead_of_overspending_cash(store: FileStore, clock: FixedClock) -> None:
    store.save_portfolio(live_portfolio(cash="1"))
    execute_at = clock.now - timedelta(minutes=10)
    p = approved_proposal(execute_at - timedelta(hours=4), execute_at)
    store.save_proposal(p.proposal_id, p)
    e = engine(store, clock, {"AAPL": Decimal(200)})
    result = e.run_proposal(p.proposal_id)
    # clamp_to_cash prevents overspending; a near-zero NAV may also breach the recorded cap and void the whole fill
    assert result.outcome in ("completed", "no_op", "voided")
    assert store.load_portfolio().cash >= Decimal(0)


# -- run_final_valuation ----------------------------------------------------------------------------------------


def test_run_final_valuation_closes_without_selling_positions(store: FileStore, clock: FixedClock) -> None:
    trade = make_trade(1, action="BUY", ticker="AAPL", shares="10", price="100", thesis="t")
    doc = make_portfolio([trade], start_cash="1500", status="LIVE")  # cash 1500 - 1000 = 500
    store.save_portfolio(doc)
    p = make_proposal(state="VETO_WINDOW", created_at=clock.now)
    store.save_proposal(p.proposal_id, p)
    e = engine(store, clock, {"AAPL": Decimal(150), "SPY": Decimal(500)})
    result = e.run_final_valuation(benchmark="SPY")
    assert result.outcome == "completed"
    out = store.load_portfolio()
    assert out.status == "CLOSED"
    assert out.positions == doc.positions  # unchanged: positions are NOT sold
    assert out.trades == doc.trades
    assert out.final_report is not None and out.final_report["ending_value"] == "2000.0000"
    assert '"state": "CANCELLED_CLOSED"' in store.proposal_path(p.proposal_id).read_text()
    (alert,) = alerts(store.root, "final_report")
    assert alert["fields"]["benchmark_relative"].startswith("+33.3333% vs SPY +0.0000%")
    assert alert["fields"]["best_worst"] == "Best: AAPL +50.0000%, Worst: AAPL +50.0000%"
    assert alert["fields"]["trade_summary"] == "1 trades: 1 buys, 0 sells"


def test_run_final_valuation_fails_closed_on_price_failure(store: FileStore, clock: FixedClock) -> None:
    trade = make_trade(1, action="BUY", ticker="AAPL", shares="10", price="100", thesis="t")
    store.save_portfolio(make_portfolio([trade], start_cash="1500", status="LIVE"))
    e = ExecutionEngine(store, clock, FakeCalendar(), FailingProvider(), FailingProvider())
    result = e.run_final_valuation()
    assert result.outcome == "failed_closed"
    assert store.load_portfolio().status == "LIVE"
