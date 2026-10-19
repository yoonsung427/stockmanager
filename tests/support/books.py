"""Test helpers for the store: fixed clock, fake PID checker, trade and portfolio factories."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from stockmanager.domain.docs import PortfolioDoc, Trade
from stockmanager.store.reconcile import rebuild_positions

PT = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 12, 6, 30, tzinfo=PT)
START = datetime(2026, 10, 12, 6, 0, tzinfo=PT)


class FixedClock:
    """Clock seam: returns a settable instant."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class FakePidChecker:
    """PidChecker seam: ``alive_pids`` maps pid -> start time of the process currently owning that pid."""

    def __init__(self) -> None:
        self.alive_pids: dict[int, datetime] = {}

    def alive(self, pid: int, start: datetime) -> bool:
        return pid in self.alive_pids and abs(self.alive_pids[pid] - start).total_seconds() <= 2

    def start_time(self, pid: int) -> datetime:
        return self.alive_pids.get(pid, START)


def make_trade(
    n: int, action: str = "BUY", ticker: str = "AAPL", shares: str = "10", price: str = "100", thesis: str = "t"
) -> Trade:
    """Trade ``P-OPEN-2026-10-12-buy<n>`` (or ``sell<n>``) with cost = shares * price."""
    cost = Decimal(shares) * Decimal(price)
    return Trade(
        trade_id=f"P-OPEN-2026-10-12-{action.lower()}{n}",
        timestamp=T0 + timedelta(minutes=n),
        action=action,  # type: ignore[arg-type]
        ticker=ticker,
        shares=Decimal(shares),
        fill_price=Decimal(price),
        cost=cost,
        price_source="yahoo",
        thesis=thesis,
    )


def make_portfolio(trades: list[Trade] | None = None, start_cash: str = "10000", status: str = "LIVE") -> PortfolioDoc:
    """A self-consistent portfolio: cash and positions derived from ``trades``."""
    trades = trades or []
    cash = Decimal(start_cash)
    for t in trades:
        cash += -t.cost if t.action == "BUY" else t.cost
    return PortfolioDoc(
        start_cash=Decimal(start_cash),
        cash=cash,
        status=status,  # type: ignore[arg-type]
        positions=rebuild_positions(trades),
        trades=trades,
    )
