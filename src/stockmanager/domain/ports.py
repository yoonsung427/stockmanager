"""Ports: Protocols implemented by adapters."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Literal, Protocol

from stockmanager.domain.docs import PriceQuote

if TYPE_CHECKING:
    from stockmanager.news.plan import NewsQuery
    from stockmanager.news.raw import RawItem


class PriceProvider(Protocol):
    """A synchronous price source. Raises ``PriceError(kind, ticker, detail)`` on any failure; never guesses."""

    name: Literal["yahoo", "finnhub"]

    def get_price(self, ticker: str, session_date: date, kind: Literal["open", "close"]) -> PriceQuote:
        """Return the ``kind`` price of ``ticker`` for ``session_date``."""
        ...


class NewsFetcher(Protocol):
    """A synchronous news source (AS §7.2). Returns headlines and snippets only; article pages are never fetched."""

    name: str  # "searxng" | "finnhub" | "yahoo_rss"

    def fetch(self, query: NewsQuery) -> list[RawItem]:
        """Return the items for ``query``. An empty list is success. Raises ``NewsUnavailable`` on any failure."""
        ...


class SessionCalendar(Protocol):
    """The NYSE calendar (AS §6.4)."""

    def next_session_open_at_or_after(self, instant: datetime) -> datetime:
        """Return the 06:30 PT open of the first session whose open is at or after ``instant``."""
        ...


class SymbolVerifier(Protocol):
    """Decides whether a ticker not currently held may be bought (AS §8.2 step 1)."""

    def is_valid_us_equity_or_etf(self, ticker: str) -> bool:
        """True if the instrument is an EQUITY or ETF, quoted in USD on a US exchange."""
        ...
