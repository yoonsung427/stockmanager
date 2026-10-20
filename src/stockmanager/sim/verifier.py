"""``FakeSymbolVerifier``: a ``SymbolVerifier`` over a fixed set of tradable tickers (TS NEG: not traded, delisted)."""

from __future__ import annotations

from collections.abc import Iterable


class FakeSymbolVerifier:
    """True only for tickers in ``valid``. Records every query."""

    def __init__(self, valid: Iterable[str]) -> None:
        self.valid = set(valid)
        self.asked: list[str] = []

    def is_valid_us_equity_or_etf(self, ticker: str) -> bool:
        """Whether ``ticker`` is in the tradable set."""
        self.asked.append(ticker)
        return ticker in self.valid
