"""Ports: Protocols implemented by adapters."""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol

from stockmanager.domain.docs import PriceQuote


class PriceProvider(Protocol):
    """A synchronous price source. Raises ``PriceError(kind, ticker, detail)`` on any failure; never guesses."""

    name: Literal["yahoo", "finnhub"]

    def get_price(self, ticker: str, session_date: date, kind: Literal["open", "close"]) -> PriceQuote:
        """Return the ``kind`` price of ``ticker`` for ``session_date``."""
        ...
