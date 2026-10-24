"""v2 H3 (R11): stale quotes propagate, but the irreversible fill path refuses them and fails closed."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest

from stockmanager.domain.docs import PriceQuote
from stockmanager.execution.engine import ExecutionEngine
from stockmanager.pricing.errors import PriceError, StalePriceRefused
from stockmanager.pricing.service import PriceService
from stockmanager.store import FileStore
from tests.support.assertions import alerts
from tests.support.books import FakePidChecker, FixedClock, make_portfolio
from tests.unit.test_execution import NOW, FakeCalendar, approved_proposal

D = date(2026, 10, 20)


class ScriptedProvider:
    """``prices`` per ticker; ``stale`` is a switch the test flips between ticks; ``down`` fails every ticker."""

    def __init__(self, name: Literal["yahoo", "finnhub"], prices: dict[str, str], *, stale: bool = False) -> None:
        self.name = name
        self.prices = prices
        self.stale = stale
        self.down = False

    def get_price(self, ticker: str, session_date: date, kind: Literal["open", "close"]) -> PriceQuote:
        if self.down:
            raise PriceError("timeout", ticker)
        return PriceQuote(ticker, session_date, kind, Decimal(self.prices[ticker]), self.name, NOW, stale=self.stale)

    def prime(self, tickers: Sequence[str], session_date: date, kind: Literal["open", "close"]) -> dict[str, Exception]:
        return {}


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def store(tmp_path: Path, clock: FixedClock) -> FileStore:
    return FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)


def _seed_approved(store: FileStore, clock: FixedClock) -> str:
    store.save_portfolio(make_portfolio(trades=[], start_cash="10000", status="LIVE"))
    execute_at = clock.now - timedelta(minutes=10)
    p = approved_proposal(execute_at - timedelta(hours=4), execute_at)
    store.save_proposal(p.proposal_id, p)
    return p.proposal_id


def test_stale_quotes_propagate_through_price_service_and_fills_can_refuse_them() -> None:
    stale, down = ScriptedProvider("yahoo", {"AAPL": "100"}, stale=True), ScriptedProvider("finnhub", {"AAPL": "100"})
    down.down = True
    # Informational callers still get the stale source, flagged.
    quotes = PriceService().fill_prices([["AAPL"]], D, stale, down)
    assert quotes["AAPL"].stale is True
    # The irreversible path does not.
    with pytest.raises(StalePriceRefused) as exc:
        PriceService().fill_prices([["AAPL"]], D, stale, down, require_fresh=True)
    assert exc.value.tickers == ["AAPL"]


def test_fresh_fallback_still_fills_when_primary_is_stale() -> None:
    stale, fresh = ScriptedProvider("yahoo", {"AAPL": "100"}, stale=True), ScriptedProvider("finnhub", {"AAPL": "101"})
    quotes = PriceService().fill_prices([["AAPL"]], D, stale, fresh, require_fresh=True)
    assert quotes["AAPL"].source == "finnhub" and not quotes["AAPL"].stale


def test_run_proposal_refuses_stale_price_and_starts_no_window(store: FileStore, clock: FixedClock) -> None:
    pid = _seed_approved(store, clock)
    primary, fallback = ScriptedProvider("yahoo", {"AAPL": "200"}, stale=True), ScriptedProvider("finnhub", {})
    fallback.down = True
    result = ExecutionEngine(store, clock, FakeCalendar(), primary, fallback).run_proposal(pid)
    assert result.outcome == "failed_closed"
    assert store.load_portfolio().trades == []  # no fill
    assert '"state": "APPROVED"' in store.proposal_path(pid).read_text()  # still fillable next tick
    assert alerts(store.root, "veto_window_open") == []  # no veto window started
    [alert] = alerts(store.root, "stale_price_refused")
    assert alert["severity"] == "info" and alert["commands"] == [] and alert["needs_action"] is False


def test_run_opening_refuses_stale_price(store: FileStore, clock: FixedClock) -> None:
    doc = make_portfolio(trades=[], start_cash="10000", status="APPROVED")
    doc = doc.model_copy(update={"config": {"staged_orders": [{"ticker": "AAPL", "target_value": "5000", "thesis": "t"}]}})
    store.save_portfolio(doc)
    primary, fallback = ScriptedProvider("yahoo", {"AAPL": "100"}, stale=True), ScriptedProvider("finnhub", {})
    fallback.down = True
    result = ExecutionEngine(store, clock, FakeCalendar(), primary, fallback).run_opening()
    assert result.outcome == "failed_closed"
    out = store.load_portfolio()
    assert out.status == "APPROVED" and out.trades == []
    assert len(alerts(store.root, "stale_price_refused")) == 1


def test_stale_refusal_self_recovers_when_a_fresh_price_arrives(store: FileStore, clock: FixedClock) -> None:
    pid = _seed_approved(store, clock)
    primary, fallback = ScriptedProvider("yahoo", {"AAPL": "200"}, stale=True), ScriptedProvider("finnhub", {})
    fallback.down = True
    engine = ExecutionEngine(store, clock, FakeCalendar(), primary, fallback)
    assert engine.run_proposal(pid).outcome == "failed_closed"
    clock.now += timedelta(minutes=1)
    assert engine.run_proposal(pid).outcome == "failed_closed"  # still stale: refused again, no second alert
    assert len(alerts(store.root, "stale_price_refused")) == 1
    primary.stale = False  # a fresh price arrives; no human step
    clock.now += timedelta(minutes=1)
    assert engine.run_proposal(pid).outcome == "completed"
    [trade] = store.load_portfolio().trades
    assert trade.fill_price == Decimal(200) and trade.price_source == "yahoo"
    assert '"state": "EXECUTED"' in store.proposal_path(pid).read_text()


def test_both_sources_failed_keeps_informational_alert_and_no_fill(store: FileStore, clock: FixedClock) -> None:
    pid = _seed_approved(store, clock)
    primary, fallback = ScriptedProvider("yahoo", {"AAPL": "200"}), ScriptedProvider("finnhub", {"AAPL": "200"})
    primary.down = fallback.down = True
    result = ExecutionEngine(store, clock, FakeCalendar(), primary, fallback).run_proposal(pid)
    assert result.outcome == "failed_closed"
    assert store.load_portfolio().trades == []
    assert alerts(store.root, "stale_price_refused") == []
    assert len(alerts(store.root, "price_source_failure")) == 1
    assert alerts(store.root, "veto_window_open") == []
    assert '"state": "APPROVED"' in store.proposal_path(pid).read_text()
