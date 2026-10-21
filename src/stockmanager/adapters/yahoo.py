"""Yahoo Finance v8 chart API price provider (primary). AS §6.2, ⚑AD2."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from stockmanager.adapters.http import USER_AGENT, HttpGet, get_with_retries, urllib_get
from stockmanager.domain.docs import PriceQuote
from stockmanager.domain.money import q4
from stockmanager.pricing.errors import PriceError
from stockmanager.pricing.validate import validate_quote

log = logging.getLogger(__name__)

# Per-request delay to avoid Yahoo rate-limiting (429) on bulk valuations.
# Yahoo free tier: ~200 req/hr; ~90 tickers per valuation (holdings + sector ETFs + benchmark).
# 20s between requests keeps us at ~45 req/hr, well under the limit.
YAHOO_RATE_DELAY = 20.0

BASE_URL = "https://query2.finance.yahoo.com/v8/finance/chart/"
QUERY = "interval=1d&range=3mo&includePrePost=false"
INSTRUMENT_TYPES = frozenset({"EQUITY", "ETF"})
SETTLE = timedelta(minutes=20)
CROSS_CHECK_TOLERANCE = Decimal("0.05")
_NY = ZoneInfo("America/New_York")


def regular_close(session_date: date) -> datetime:
    """16:00 ET on ``session_date``. Ignores early closes until the calendar is injected (AS §6.4)."""
    return datetime.combine(session_date, dtime(16, 0), tzinfo=_NY)


class YahooPriceProvider:
    """Prices from the daily bar of the requested session, never from ``meta.regularMarketPrice``.

    ``now`` is the injected clock (AS P8, must be tz-aware) and ``session_close`` maps a session date to its close
    instant (the calendar). ``regularMarketPrice`` is only a cross-check that logs a warning.
    """

    name: Literal["yahoo", "finnhub"] = "yahoo"

    def __init__(
        self,
        now: Callable[[], datetime],
        session_close: Callable[[date], datetime] = regular_close,
        http_get: HttpGet = urllib_get,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._now = now
        self._session_close = session_close
        self._get = http_get
        self._sleep = sleep

    def get_price(self, ticker: str, session_date: date, kind: Literal["open", "close"]) -> PriceQuote:
        """Return the open or close of ``ticker`` for ``session_date`` from its daily bar."""
        now = self._now()
        if kind == "close" and now < self._session_close(session_date) + SETTLE:
            raise PriceError("incomplete_session", ticker, f"{session_date} close not settled at {now.isoformat()}")
        url = f"{BASE_URL}{ticker.upper()}?{QUERY}"
        body = get_with_retries(
            self._get, url, {"User-Agent": USER_AGENT, "Accept": "application/json"}, ticker, self._sleep
        )
        result = _result(body, ticker)
        meta = _dict(result.get("meta"), ticker, "meta")
        _check_instrument(meta, ticker)
        tz = _exchange_tz(meta, ticker)
        timestamps = result.get("timestamp")
        if not isinstance(timestamps, list) or not all(isinstance(t, int) for t in timestamps):
            raise PriceError("malformed", ticker, "timestamp[] missing or not integers")
        dates = [datetime.fromtimestamp(t, UTC).astimezone(tz).date() for t in timestamps]
        series = _series(result, kind, len(dates), ticker)
        if session_date not in dates:
            raise PriceError(
                "stale_date" if dates and session_date > dates[-1] else "missing", ticker, f"no bar for {session_date}"
            )
        i = dates.index(session_date)
        raw = series[i]
        if raw is None:
            raise PriceError("missing", ticker, f"{kind} is null for {session_date}")
        if not isinstance(raw, int | Decimal) or isinstance(raw, bool):
            raise PriceError("malformed", ticker, f"{kind} is not a number: {raw!r}")
        raw = Decimal(raw)
        if not raw.is_finite():
            raise PriceError("non_finite", ticker, str(raw))
        quote = validate_quote(PriceQuote(ticker, session_date, kind, q4(raw), "yahoo", now))
        if kind == "close" and i == len(dates) - 1:
            _cross_check(meta, quote)
        return quote


def _result(body: bytes, ticker: str) -> dict[str, Any]:
    try:
        doc = json.loads(body, parse_float=Decimal, parse_constant=Decimal)
        chart = _dict(doc, ticker, "response")["chart"]
        if chart.get("error") is not None:
            raise PriceError("malformed", ticker, f"chart.error: {chart['error']}")
        results = chart["result"]
        if not isinstance(results, list) or len(results) != 1:
            raise PriceError("malformed", ticker, "chart.result is not a single-element list")
        return _dict(results[0], ticker, "chart.result[0]")
    except (ValueError, KeyError, AttributeError, TypeError) as e:
        raise PriceError("malformed", ticker, f"unparseable response: {type(e).__name__}: {e}") from e


def _dict(value: object, ticker: str, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PriceError("malformed", ticker, f"{what} is not an object")
    return value


def _check_instrument(meta: dict[str, Any], ticker: str) -> None:
    if meta.get("currency") != "USD":
        raise PriceError("malformed", ticker, f"currency {meta.get('currency')!r}, expected USD")
    if meta.get("instrumentType") not in INSTRUMENT_TYPES:
        raise PriceError("malformed", ticker, f"instrumentType {meta.get('instrumentType')!r}")


def _exchange_tz(meta: Mapping[str, Any], ticker: str) -> ZoneInfo:
    name = meta.get("exchangeTimezoneName")
    try:
        return ZoneInfo(name)  # type: ignore[arg-type]
    except (ZoneInfoNotFoundError, ValueError, TypeError) as e:
        raise PriceError("malformed", ticker, f"exchangeTimezoneName {name!r}") from e


def _series(result: Mapping[str, Any], kind: str, n: int, ticker: str) -> list[Any]:
    try:
        series = result["indicators"]["quote"][0][kind]
    except (KeyError, IndexError, TypeError) as e:
        raise PriceError("malformed", ticker, f"indicators.quote[0].{kind}[] missing") from e
    if not isinstance(series, list) or len(series) != n:
        raise PriceError("malformed", ticker, f"{kind}[] does not line up with timestamp[]")
    return series


def _cross_check(meta: Mapping[str, Any], quote: PriceQuote) -> None:
    live = meta.get("regularMarketPrice")
    if not isinstance(live, Decimal) or not live.is_finite():
        return
    if abs(live - quote.price) / quote.price > CROSS_CHECK_TOLERANCE:
        log.warning(
            "yahoo %s: regularMarketPrice %s differs from %s bar close %s by more than %s",
            quote.ticker,
            live,
            quote.session_date,
            quote.price,
            CROSS_CHECK_TOLERANCE,
        )


def _parse_batch(
    body: bytes, tickers: list[str], session_date: date, kind: Literal["open", "close"], now: datetime
) -> dict[str, PriceQuote]:
    """Parse a batch Yahoo chart response into PriceQuotes."""
    try:
        doc = json.loads(body, parse_float=Decimal, parse_constant=Decimal)
        root = _dict(doc, tickers[0], "response")
        chart = root["chart"]
    except (ValueError, KeyError, TypeError) as e:
        raise PriceError("malformed", tickers[0], f"unparseable response: {type(e).__name__}: {e}") from e
    if chart.get("error") is not None:
        raise PriceError("malformed", tickers[0], f"chart.error: {chart['error']}")
    results = chart["result"]
    if not isinstance(results, list) or len(results) == 0:
        raise PriceError("malformed", tickers[0], "chart.result is empty or not a list")

    quotes: dict[str, PriceQuote] = {}
    for result in results:
        if not isinstance(result, dict):
            # Yahoo returns null for unknown symbols — skip
            continue
        meta = _dict(result.get("meta"), tickers[0], "meta")
        symbol = meta.get("symbol", "")
        if not symbol or symbol not in {t.upper() for t in tickers}:
            continue
        _check_instrument(meta, symbol)
        tz = _exchange_tz(meta, symbol)
        timestamps = result.get("timestamp")
        if not isinstance(timestamps, list) or not all(isinstance(t, int) for t in timestamps):
            raise PriceError("malformed", symbol, "timestamp[] missing or not integers")
        dates = [datetime.fromtimestamp(t, UTC).astimezone(tz).date() for t in timestamps]
        series = _series(result, kind, len(dates), symbol)
        if session_date not in dates:
            raise PriceError(
                "stale_date" if dates and session_date > dates[-1] else "missing",
                symbol, f"no bar for {session_date}"
            )
        i = dates.index(session_date)
        raw = series[i]
        if raw is None:
            raise PriceError("missing", symbol, f"{kind} is null for {session_date}")
        if not isinstance(raw, int | Decimal) or isinstance(raw, bool):
            raise PriceError("malformed", symbol, f"{kind} is not a number: {raw!r}")
        raw = Decimal(raw)
        if not raw.is_finite():
            raise PriceError("non_finite", symbol, str(raw))
        quote = validate_quote(PriceQuote(symbol, session_date, kind, q4(raw), "yahoo", now))
        if kind == "close" and i == len(dates) - 1:
            _cross_check(meta, quote)
        quotes[symbol] = quote
    return quotes
