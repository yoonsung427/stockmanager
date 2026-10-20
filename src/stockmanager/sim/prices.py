"""``ScriptedPriceProvider``: a ``PriceProvider`` whose answer per ticker is scripted (TS §0.3)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from stockmanager.domain.docs import PriceQuote
from stockmanager.pricing.errors import PriceError, PriceErrorKind

#: A price (returned as-is, so ``0``/negative/NaN exercise the service's validation) or an error kind to raise.
Script = Decimal | PriceErrorKind


class ScriptedPriceProvider:
    """Answers ``get_price`` from ``script``; a ticker that is not scripted is ``missing``. Records every call."""

    def __init__(
        self,
        name: Literal["yahoo", "finnhub"],
        script: Mapping[str, Script],
        fetched_at: Callable[[], datetime],
    ) -> None:
        self.name = name
        self.script: dict[str, Script] = dict(script)
        self._fetched_at = fetched_at
        self.calls: list[tuple[str, date, str]] = []

    def get_price(self, ticker: str, session_date: date, kind: Literal["open", "close"]) -> PriceQuote:
        """Return the scripted quote or raise ``PriceError`` of the scripted kind."""
        self.calls.append((ticker, session_date, kind))
        step = self.script.get(ticker, "missing")
        if isinstance(step, str):
            raise PriceError(step, ticker, "scripted")
        return PriceQuote(ticker, session_date, kind, step, self.name, self._fetched_at())

    def fail(self, *tickers: str, kind: PriceErrorKind = "timeout") -> None:
        """Make every named ticker fail with ``kind`` from now on."""
        for t in tickers:
            self.script[t] = kind

    def fail_all(self, kind: PriceErrorKind = "timeout") -> None:
        """Make every scripted ticker (and none other) fail with ``kind``."""
        for t in self.script:
            self.script[t] = kind
