"""Pure fill math (AS §1.3, §8.6): the only arithmetic for position and cash changes.

Shares and cash are ``Decimal``, 4 dp, ``ROUND_HALF_UP`` (TS D1). Cost basis uses the average-cost method: a sell
removes ``cost_basis * sold / held`` (4 dp, half-up) from the basis; a position reaching zero shares is removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from stockmanager.domain.docs import Position, Trade
from stockmanager.domain.money import Q4, q4

ZERO = Decimal("0.0000")


def size_buy(budget: Decimal, price: Decimal) -> Decimal:
    """Shares a BUY fills for ``budget`` dollars at ``price``, rounded half-up to 4 dp (TS D1)."""
    if price <= 0:
        raise ValueError("price must be positive")
    if budget <= 0:
        return ZERO
    return q4(budget / price)


def clamp_to_cash(shares: Decimal, price: Decimal, available: Decimal) -> Decimal:
    """Reduce (never increase) ``shares`` so ``shares * price <= available`` (⚑AD14).

    Half-up rounding in ``size_buy`` can overshoot the budget by a tick; this guarantees cash never goes negative.
    """
    if shares <= 0 or price <= 0:
        return ZERO
    if shares * price <= available:
        return shares
    if available <= 0:
        return ZERO
    capped = (available / price).quantize(Q4, rounding=ROUND_DOWN)
    while capped > 0 and capped * price > available:
        capped -= Q4
    return capped if capped > 0 else ZERO


def size_sell(held: Decimal, fraction: Decimal) -> Decimal:
    """Shares a SELL fills: ``held * fraction``, rounded half-up to 4 dp, never more than ``held``."""
    if held <= 0 or fraction <= 0:
        return ZERO
    return min(q4(held * fraction), held)


@dataclass(frozen=True)
class LedgerState:
    """Cash and open positions, keyed by ticker: the state ``apply_fill`` folds over."""

    cash: Decimal
    positions: dict[str, Position]


def apply_fill(state: LedgerState, trade: Trade) -> LedgerState:
    """The ONLY position/cash arithmetic (AS §1.3). Average-cost method; a position at 0 shares is deleted.

    Raises ``ValueError`` for a SELL of more shares than held (long-only, oversell guard).
    """
    positions = dict(state.positions)
    cur = positions.get(trade.ticker)
    if trade.action == "BUY":
        cash = q4(state.cash - trade.cost)
        shares = q4((cur.shares if cur else Decimal(0)) + trade.shares)
        basis = q4((cur.cost_basis if cur else Decimal(0)) + trade.cost)
        positions[trade.ticker] = Position(
            ticker=trade.ticker,
            shares=shares,
            fill_price=q4(basis / shares),
            fill_time=cur.fill_time if cur else trade.timestamp,
            cost_basis=basis,
            thesis=trade.thesis or (cur.thesis if cur else ""),
            price_source=trade.price_source,
        )
    else:
        if cur is None or trade.shares > cur.shares:
            raise ValueError(f"sell {trade.shares} {trade.ticker} exceeds held {cur.shares if cur else 0}")
        cash = q4(state.cash + trade.cost)
        remaining = q4(cur.shares - trade.shares)
        if remaining == 0:
            del positions[trade.ticker]
        else:
            basis = q4(cur.cost_basis * remaining / cur.shares)
            positions[trade.ticker] = cur.model_copy(
                update={
                    "shares": remaining,
                    "cost_basis": basis,
                    "fill_price": q4(basis / remaining),
                    "price_source": trade.price_source,
                }
            )
    return LedgerState(cash=cash, positions=positions)
