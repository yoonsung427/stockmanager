"""Shared plumbing for the summary jobs: ports, the ``last_close.json`` state file, holdings and price helpers."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from stockmanager.domain.docs import PortfolioDoc, PriceQuote
from stockmanager.domain.money import q4
from stockmanager.domain.ports import NewsFetcher, PriceProvider
from stockmanager.llm.summary import NewsItem, PositionInfo
from stockmanager.news.config import NewsConfig
from stockmanager.news.lookup import recent_news
from stockmanager.pricing.service import PriceService

log = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
LAST_CLOSE_NAME = "last_close.json"

#: ``(ticker, news_items, position_info, session_date) -> summary text``; raises ``SummaryError``.
SummaryGen = Callable[[str, Sequence[NewsItem], PositionInfo, date], str]
#: ``(ticker, session_date) -> recent headlines``; never raises.
NewsLookup = Callable[[str, date], Sequence[NewsItem]]


class TradingCalendar(Protocol):
    """The slice of ``JsonCalendar`` the summary jobs need."""

    def is_session(self, day: date) -> bool:
        """True if the exchange is open on ``day``."""
        ...

    def session_open(self, day: date) -> datetime:
        """The open instant of the session on ``day``."""
        ...

    def session_close(self, day: date) -> datetime:
        """The close instant of the session on ``day`` (early-close aware)."""
        ...


@dataclass(frozen=True)
class Holding:
    """One ticker's position, aggregated over its lots."""

    ticker: str
    shares: Decimal
    cost_basis: Decimal
    thesis: str


def et_date(now: datetime) -> date:
    """The New York calendar date of ``now``."""
    return now.astimezone(NY).date()


def holdings(portfolio: PortfolioDoc) -> list[Holding]:
    """The portfolio's positions as one ``Holding`` per ticker, in first-seen order."""
    out: dict[str, Holding] = {}
    for p in portfolio.positions:
        prev = out.get(p.ticker)
        out[p.ticker] = Holding(
            p.ticker,
            p.shares + (prev.shares if prev else Decimal(0)),
            p.cost_basis + (prev.cost_basis if prev else Decimal(0)),
            prev.thesis if prev and prev.thesis else p.thesis,
        )
    return list(out.values())


def position_info(
    h: Holding, price: Decimal | None, price_date: date | None, previous_close: Decimal | None
) -> PositionInfo:
    """``PositionInfo`` for ``h``; ``day_change_pct`` is vs ``previous_close`` when both prices are known."""
    return PositionInfo(h.shares, h.cost_basis, price, price_date, move_pct(price, previous_close), h.thesis)


def move_pct(price: Decimal | None, previous: Decimal | None) -> Decimal | None:
    """``(price - previous) / previous`` in percent, 4 dp; ``None`` if either is missing or ``previous`` is 0."""
    if price is None or previous is None or previous == 0:
        return None
    return q4((price - previous) / previous * 100)


# -- state/last_close.json -----------------------------------------------------------------------------------------


def last_close_path(root: Path) -> Path:
    """``<root>/state/last_close.json``."""
    return root / "state" / LAST_CLOSE_NAME


def load_last_close(root: Path) -> dict[str, Decimal]:
    """Previous-close prices by ticker. An absent, unparsable or partly bad file yields what could be read."""
    path = last_close_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Decimal] = {}
    for ticker, value in data.items():
        try:
            price = Decimal(str(value))
        except InvalidOperation:
            continue
        if isinstance(ticker, str) and price.is_finite() and price > 0:
            out[ticker] = price
    return out


def dump_last_close(closes: Mapping[str, Decimal]) -> str:
    """Canonical text for ``last_close.json``: ``{"MSFT": 432.1000, ...}``.

    Prices are written as JSON numbers (the agreed shape) straight from ``Decimal`` at 4 dp, never through ``float``.
    """
    if not closes:
        return "{}\n"
    rows = (f"  {json.dumps(t, ensure_ascii=False)}: {q4(p)}" for t, p in sorted(closes.items()))
    return "{\n" + ",\n".join(rows) + "\n}\n"


# -- prices and news -----------------------------------------------------------------------------------------------


def priced(
    quotes: Mapping[str, PriceQuote],
) -> dict[str, Decimal]:
    """``{ticker: price}`` from one valuation's quotes."""
    return {t: q.price for t, q in quotes.items()}


def close_prices(
    tickers: Sequence[str], session_date: date, primary: PriceProvider, fallback: PriceProvider | None
) -> dict[str, Decimal]:
    """Closes for ``session_date`` from ONE source (primary, else fallback; never mixed). Raises ``PriceError`` family."""
    table = PriceService().valuation(tickers, session_date, primary, fallback or primary)
    return priced(table.quotes)


def open_prices(
    tickers: Sequence[str], session_date: date, primary: PriceProvider, fallback: PriceProvider | None
) -> dict[str, Decimal]:
    """Opening prices for ``session_date`` from ONE source. Raises ``ValuationUnavailable`` if neither can price all."""
    return priced(PriceService().fill_prices([list(tickers)], session_date, primary, fallback or primary))


def latest_closed_session(calendar: TradingCalendar, now: datetime, settle: timedelta = timedelta(minutes=20)) -> date:
    """The most recent session whose close (plus ``settle``) is at or before ``now``."""
    day = et_date(now)
    for _ in range(14):
        if calendar.is_session(day) and calendar.session_close(day) + settle <= now:
            return day
        day -= timedelta(days=1)
    raise ValueError("no completed session in the last 14 days")


def make_news_lookup(
    fetchers: Sequence[NewsFetcher], news_config: NewsConfig, *, lookback_days: int = 3
) -> NewsLookup:
    """Bind the fetchers into a ``NewsLookup`` that returns ``llm.summary.NewsItem`` records."""

    def lookup(ticker: str, session_date: date) -> list[NewsItem]:
        items = recent_news(fetchers, news_config, ticker, session_date, lookback_days=lookback_days)
        return [NewsItem(i.title, i.snippet, i.domain, i.published) for i in items]

    return lookup


def safe_news(lookup: NewsLookup, ticker: str, session_date: date) -> list[NewsItem]:
    """``lookup`` that can never raise: news is context, its absence must not stop a job."""
    try:
        return list(lookup(ticker, session_date))
    except Exception:
        log.exception("news lookup failed for %s", ticker)
        return []
