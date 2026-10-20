"""``SymbolVerifier`` (AS §8.2 step 1, ⚑cap table row 1): is a not-yet-held ticker a buyable US equity or ETF.

Reuses Yahoo's v8 chart API ``meta`` block (the same endpoint ``YahooPriceProvider`` uses for prices), asking only
whether the instrument is an ``EQUITY``/``ETF`` quoted in ``USD``. Any failure (network, malformed body, unknown
symbol) is treated as "not valid": a buy of an unverifiable symbol must never slip through (fail closed).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from decimal import Decimal

from stockmanager.adapters.http import USER_AGENT, HttpGet, get_with_retries, urllib_get
from stockmanager.adapters.yahoo import BASE_URL, INSTRUMENT_TYPES
from stockmanager.pricing.errors import PriceError


class YahooSymbolVerifier:
    """Checks ``ticker`` against Yahoo's instrument metadata; never consults price history."""

    def __init__(self, http_get: HttpGet = urllib_get, sleep: Callable[[float], None] = time.sleep) -> None:
        self._get = http_get
        self._sleep = sleep

    def is_valid_us_equity_or_etf(self, ticker: str) -> bool:
        """True iff Yahoo reports ``ticker`` as a USD-quoted ``EQUITY``/``ETF``. Fails closed on any error."""
        try:
            url = f"{BASE_URL}{ticker.upper()}?range=5d&interval=1d"
            body = get_with_retries(
                self._get, url, {"User-Agent": USER_AGENT, "Accept": "application/json"}, ticker, self._sleep
            )
            doc = json.loads(body, parse_float=Decimal, parse_constant=Decimal)
            results = doc["chart"]["result"]
            if doc["chart"].get("error") is not None or not isinstance(results, list) or len(results) != 1:
                return False
            meta = results[0]["meta"]
            return meta.get("currency") == "USD" and meta.get("instrumentType") in INSTRUMENT_TYPES
        except (PriceError, ValueError, KeyError, TypeError, IndexError):
            return False
