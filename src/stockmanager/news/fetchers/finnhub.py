"""Finnhub news adapter (fallback). The token goes in the ``X-Finnhub-Token`` header, never the URL.

Endpoints: ``/company-news`` (holding, analyst, benchmark, sector), ``/news?category=general`` (macro) and
``/calendar/earnings`` (earnings; one synthetic item per row). Responses are memoised per
``(endpoint, symbol, from, to)`` for the life of the instance, which is one cycle: a holding's ``holding`` and
``analyst`` queries share one request.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode

from stockmanager.adapters.http import HttpGet
from stockmanager.news.config import TICKER_RE
from stockmanager.news.fetchers.base import USER_AGENT, NewsUnavailable, default_get, domain_of, fetch_body
from stockmanager.news.plan import NewsQuery
from stockmanager.news.raw import SNIPPET_MAX_CHARS, TITLE_MAX_CHARS, RawItem, clip

BASE_URL = "https://finnhub.io/api/v1"
_HOST = "finnhub.io"
_SLUG_RE = re.compile(r"[a-z0-9.\-]{1,64}")
_Key = tuple[str, str, str, str]


class FinnhubNewsFetcher:
    """Structured fallback. A missing key fails every call with ``kind="auth"`` so the breaker opens at once."""

    name: str = "finnhub"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        http_get: HttpGet = default_get,
        sleep: Callable[[float], None] = time.sleep,
        throttle: Callable[[], object] = lambda: None,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("FINNHUB_API_KEY", "")
        self._get = http_get
        self._sleep = sleep
        self._throttle = throttle
        self._memo: dict[_Key, list[RawItem]] = {}
        self._memo_lock = threading.Lock()
        self._key_locks: dict[_Key, threading.Lock] = {}

    def fetch(self, query: NewsQuery) -> list[RawItem]:
        """Fetch the endpoint that serves ``query.kind``. Raises ``NewsUnavailable`` on any failure."""
        if not self._api_key:
            raise NewsUnavailable("FINNHUB_API_KEY is not set", kind="auth")
        if query.kind == "macro":
            key: _Key = ("news", "", "", "")
        elif query.kind == "earnings":
            key = ("calendar/earnings", query.ticker, query.window_from.isoformat(), query.window_to.isoformat())
        else:
            key = ("company-news", query.ticker, query.window_from.isoformat(), query.window_to.isoformat())
        with self._memo_lock:
            lock = self._key_locks.setdefault(key, threading.Lock())
        with lock:  # concurrent queries for the same key make one request
            cached = self._memo.get(key)
            if cached is None:
                cached = self._request(key, query)
                self._memo[key] = cached
        return list(cached)

    def _request(self, key: _Key, query: NewsQuery) -> list[RawItem]:
        endpoint, symbol, start, end = key
        if endpoint == "news":
            url = f"{BASE_URL}/news?{urlencode({'category': 'general'})}"
        elif endpoint == "calendar/earnings":
            url = f"{BASE_URL}/calendar/earnings?{urlencode({'from': start, 'to': end, 'symbol': symbol})}"
        else:
            url = f"{BASE_URL}/company-news?{urlencode({'symbol': symbol, 'from': start, 'to': end})}"
        headers = {"X-Finnhub-Token": self._api_key, "User-Agent": USER_AGENT, "Accept": "application/json"}
        body = fetch_body(
            self._get, url, headers, before_attempt=self._throttle, sleep=self._sleep, retry_429_wait=10.0
        )
        try:
            doc = json.loads(body)
        except ValueError as e:
            raise NewsUnavailable("malformed body: not JSON") from e
        if endpoint == "calendar/earnings":
            return _calendar_items(doc, query.ticker, url)
        if not isinstance(doc, list):
            raise NewsUnavailable("malformed body: expected a list")
        return [_article(a) for a in doc]


def _article(entry: Any) -> RawItem:
    a = entry if isinstance(entry, dict) else {}
    url = _text(a.get("url"))
    domain = domain_of(url)
    if domain == _HOST or domain.endswith("." + _HOST):
        domain = _slug(_text(a.get("source")))
    return RawItem(
        title=clip(_text(a.get("headline")), TITLE_MAX_CHARS),
        snippet=clip(_text(a.get("summary")), SNIPPET_MAX_CHARS),
        url=url,
        published=_unix_date(a.get("datetime")),
        domain=domain,
    )


def _calendar_items(doc: Any, ticker: str, url: str) -> list[RawItem]:
    """One template-built item per row; no vendor text is used (title from a validated symbol and parsed date)."""
    rows = doc.get("earningsCalendar") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise NewsUnavailable("malformed body: no earningsCalendar list")
    items: list[RawItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        try:
            when = date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if symbol != ticker or not TICKER_RE.fullmatch(symbol):
            continue
        items.append(RawItem(f"{symbol} earnings scheduled {when.isoformat()}", "", url, None, _HOST))
    return items


def _slug(source: str) -> str:
    slug = re.sub(r"[^a-z0-9.]+", "-", source.lower()).strip("-.")
    return slug if _SLUG_RE.fullmatch(slug) else _HOST


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _unix_date(value: Any) -> date | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value, UTC).date()
    except (OverflowError, OSError, ValueError):
        return None
