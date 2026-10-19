"""Finnhub price provider (fallback). AS §6.2.

``/quote`` serves only the latest session, so older session dates are ``stale_date``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from stockmanager.adapters.http import USER_AGENT, HttpGet, get_with_retries, urllib_get
from stockmanager.adapters.yahoo import SETTLE, regular_close
from stockmanager.domain.docs import PriceQuote
from stockmanager.domain.money import q4
from stockmanager.pricing.errors import PriceError
from stockmanager.pricing.validate import validate_quote

BASE_URL = "https://finnhub.io/api/v1/quote"
_NY = ZoneInfo("America/New_York")


class FinnhubPriceProvider:
    """The API key is sent in the ``X-Finnhub-Token`` header, never in URLs or logs.

    Not yet done: the 30/min client-side limiter, and CT-FH-1 (whether ``c`` includes extended hours).
    """

    name: Literal["yahoo", "finnhub"] = "finnhub"

    def __init__(
        self,
        api_key: str,
        now: Callable[[], datetime],
        session_close: Callable[[date], datetime] = regular_close,
        http_get: HttpGet = urllib_get,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self._now = now
        self._session_close = session_close
        self._get = http_get
        self._sleep = sleep

    def get_price(self, ticker: str, session_date: date, kind: Literal["open", "close"]) -> PriceQuote:
        """Return ``o`` (open) or ``c`` (close) if the last trade ``t`` falls on ``session_date``."""
        now = self._now()
        close_at = self._session_close(session_date)
        if kind == "close" and now < close_at + SETTLE:
            raise PriceError("incomplete_session", ticker, f"{session_date} close not settled at {now.isoformat()}")
        headers = {"X-Finnhub-Token": self._api_key, "User-Agent": USER_AGENT, "Accept": "application/json"}
        body = get_with_retries(self._get, f"{BASE_URL}?symbol={ticker.upper()}", headers, ticker, self._sleep)
        try:
            doc = json.loads(body, parse_float=Decimal, parse_constant=Decimal)
            raw, traded = doc["c" if kind == "close" else "o"], doc["t"]
        except (ValueError, KeyError, TypeError) as e:
            raise PriceError("malformed", ticker, f"unparseable response: {type(e).__name__}") from e
        if not isinstance(raw, int | Decimal) or isinstance(raw, bool) or not isinstance(traded, int):
            raise PriceError("malformed", ticker, "price or t is not a number")
        price = Decimal(raw)
        if not price.is_finite():
            raise PriceError("non_finite", ticker, str(price))
        # Finnhub answers unknown symbols with zeros, so positivity is checked before the date.
        quote = validate_quote(PriceQuote(ticker, session_date, kind, q4(price), "finnhub", now))
        last_trade = datetime.fromtimestamp(traded, UTC)
        if last_trade.astimezone(_NY).date() != session_date:
            raise PriceError("stale_date", ticker, f"last trade {last_trade.astimezone(_NY).isoformat()}")
        if kind == "close" and last_trade > close_at + SETTLE:
            raise PriceError("incomplete_session", ticker, "last trade is after the settle window (extended hours?)")
        return quote
