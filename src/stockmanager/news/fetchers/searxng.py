"""SearXNG news adapter (primary). ``GET {base_url}/search?q=..&categories=news&format=json&time_range=..``.

``format=json`` must be enabled in the instance's ``settings.yml``. Only the search API is called.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

from stockmanager.adapters.http import HttpGet
from stockmanager.news.fetchers.base import USER_AGENT, NewsUnavailable, default_get, domain_of, fetch_body
from stockmanager.news.plan import NewsQuery
from stockmanager.news.raw import SNIPPET_MAX_CHARS, TITLE_MAX_CHARS, RawItem, clip


class SearxngFetcher:
    """Keyword search; serves every query kind. An empty result list is success (no fallback)."""

    name: str = "searxng"

    def __init__(
        self,
        base_url: str,
        *,
        http_get: HttpGet = default_get,
        sleep: Callable[[float], None] = time.sleep,
        throttle: Callable[[], object] = lambda: None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._get = http_get
        self._sleep = sleep
        self._throttle = throttle

    def fetch(self, query: NewsQuery) -> list[RawItem]:
        """Search ``query.text`` as news. Raises ``NewsUnavailable`` on timeout, connection error, 5xx or a bad body."""
        params = {"q": query.text, "categories": "news", "format": "json", "time_range": _time_range(query)}
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        body = fetch_body(
            self._get,
            f"{self._base}/search?{urlencode(params)}",
            headers,
            before_attempt=self._throttle,
            sleep=self._sleep,
        )
        try:
            doc = json.loads(body)
        except ValueError as e:
            raise NewsUnavailable("malformed body: not JSON") from e
        results = doc.get("results") if isinstance(doc, dict) else None
        if not isinstance(results, list):
            raise NewsUnavailable("malformed body: no results list")
        return [_item(r) for r in results]


def _time_range(query: NewsQuery) -> str:
    days = (query.window_to - query.window_from).days
    return "day" if days <= 1 else "week" if days <= 7 else "month"


def _item(result: Any) -> RawItem:
    """Map one result. Missing fields become empty strings so normalisation rejects the item with a reason code."""
    r = result if isinstance(result, dict) else {}
    url = _text(r.get("url"))
    return RawItem(
        title=clip(_text(r.get("title")), TITLE_MAX_CHARS),
        snippet=clip(_text(r.get("content")), SNIPPET_MAX_CHARS),
        url=url,
        published=_parse_date(r.get("publishedDate")),
        domain=domain_of(url),
    )


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None
