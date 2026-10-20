"""Pure fill math (AS §1.3, §8.6): size_buy, clamp_to_cash, size_sell, apply_fill."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from stockmanager.domain.docs import Position, Trade
from stockmanager.domain.fills import LedgerState, apply_fill, clamp_to_cash, size_buy, size_sell

PT = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 12, 6, 30, tzinfo=PT)


def trade(action: str, ticker: str, shares: str, price: str, thesis: str = "") -> Trade:
    s, p = Decimal(shares), Decimal(price)
    return Trade(
        trade_id=f"T-{action}-{ticker}",
        timestamp=T0,
        action=action,  # type: ignore[arg-type]
        ticker=ticker,
        shares=s,
        fill_price=p,
        cost=s * p,
        price_source="yahoo",
        thesis=thesis,
    )


# -- size_buy -------------------------------------------------------------------------------------------------


def test_size_buy_rounds_half_up_to_4dp() -> None:
    assert size_buy(Decimal("100.00999"), Decimal(100)) == Decimal("1.0001")
    assert size_buy(Decimal(1000), Decimal(3)) == Decimal("333.3333")


def test_size_buy_zero_or_negative_budget_is_zero() -> None:
    assert size_buy(Decimal(0), Decimal(100)) == Decimal("0.0000")
    assert size_buy(Decimal(-5), Decimal(100)) == Decimal("0.0000")


def test_size_buy_rejects_non_positive_price() -> None:
    with pytest.raises(ValueError, match="positive"):
        size_buy(Decimal(100), Decimal(0))


# -- clamp_to_cash ----------------------------------------------------------------------------------------------


def test_clamp_to_cash_passes_through_when_affordable() -> None:
    assert clamp_to_cash(Decimal(10), Decimal(5), Decimal(100)) == Decimal(10)


def test_clamp_to_cash_reduces_shares_to_fit_available() -> None:
    # size_buy(100, 3) rounds up to 33.3333, costing 99.9999 -- but only 99 is available
    shares = size_buy(Decimal(100), Decimal(3))
    clamped = clamp_to_cash(shares, Decimal(3), Decimal(99))
    assert clamped * Decimal(3) <= Decimal(99)
    assert clamped == Decimal("33.0000")


def test_clamp_to_cash_zero_or_negative_available_is_zero() -> None:
    assert clamp_to_cash(Decimal(10), Decimal(5), Decimal(0)) == Decimal("0.0000")
    assert clamp_to_cash(Decimal(10), Decimal(5), Decimal(-1)) == Decimal("0.0000")


def test_clamp_to_cash_never_increases_shares() -> None:
    assert clamp_to_cash(Decimal(10), Decimal(5), Decimal(1000)) == Decimal(10)


# -- size_sell --------------------------------------------------------------------------------------------------


def test_size_sell_rounds_half_up_and_never_exceeds_held() -> None:
    assert size_sell(Decimal(100), Decimal("0.125")) == Decimal("12.5000")
    assert size_sell(Decimal(10), Decimal("1.5")) == Decimal(10)  # fraction > 1: never more than held


def test_size_sell_non_positive_inputs_are_zero() -> None:
    assert size_sell(Decimal(0), Decimal("0.5")) == Decimal("0.0000")
    assert size_sell(Decimal(10), Decimal(0)) == Decimal("0.0000")


# -- apply_fill: average-cost method -------------------------------------------------------------------------


def test_apply_fill_buy_opens_a_new_position() -> None:
    state = LedgerState(cash=Decimal(10000), positions={})
    out = apply_fill(state, trade("BUY", "AAPL", "10", "100", "t"))
    pos = out.positions["AAPL"]
    assert (out.cash, pos.shares, pos.cost_basis, pos.fill_price) == (Decimal(9000), Decimal(10), Decimal(1000), Decimal(100))


def test_apply_fill_buy_averages_cost_basis() -> None:
    state = LedgerState(cash=Decimal(10000), positions={})
    state = apply_fill(state, trade("BUY", "AAPL", "10", "100", "first"))
    state = apply_fill(state, trade("BUY", "AAPL", "10", "200"))
    pos = state.positions["AAPL"]
    # 10@100 + 10@200 = 20 shares, cost basis 3000, avg 150
    assert (pos.shares, pos.cost_basis, pos.fill_price) == (Decimal(20), Decimal(3000), Decimal(150))
    assert pos.thesis == "first"  # a later BUY with no thesis keeps the earlier one


def test_apply_fill_sell_reduces_cost_basis_proportionally() -> None:
    state = LedgerState(cash=Decimal(0), positions={"AAPL": Position(
        ticker="AAPL", shares=Decimal(20), fill_price=Decimal(100), fill_time=T0, cost_basis=Decimal(2000),
        thesis="t", price_source="yahoo",
    )})
    out = apply_fill(state, trade("SELL", "AAPL", "5", "150"))
    pos = out.positions["AAPL"]
    # sell 5 of 20: remaining basis = 2000 * 15/20 = 1500
    assert (out.cash, pos.shares, pos.cost_basis, pos.fill_price) == (Decimal(750), Decimal(15), Decimal(1500), Decimal(100))


def test_apply_fill_sell_to_zero_deletes_the_position() -> None:
    state = LedgerState(cash=Decimal(0), positions={"AAPL": Position(
        ticker="AAPL", shares=Decimal(10), fill_price=Decimal(100), fill_time=T0, cost_basis=Decimal(1000),
        thesis="t", price_source="yahoo",
    )})
    out = apply_fill(state, trade("SELL", "AAPL", "10", "120"))
    assert "AAPL" not in out.positions
    assert out.cash == Decimal(1200)


def test_apply_fill_oversell_raises() -> None:
    state = LedgerState(cash=Decimal(0), positions={"AAPL": Position(
        ticker="AAPL", shares=Decimal(5), fill_price=Decimal(100), fill_time=T0, cost_basis=Decimal(500),
        thesis="t", price_source="yahoo",
    )})
    with pytest.raises(ValueError, match="exceeds held"):
        apply_fill(state, trade("SELL", "AAPL", "6", "100"))


def test_apply_fill_sell_of_unheld_ticker_raises() -> None:
    state = LedgerState(cash=Decimal(0), positions={})
    with pytest.raises(ValueError, match="exceeds held"):
        apply_fill(state, trade("SELL", "AAPL", "1", "100"))
