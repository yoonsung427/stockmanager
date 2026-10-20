"""Yahoo Finance RSS adapter (last resort). Unofficial and undocumented: best effort, per-ticker only.

Parsed with ``defusedxml`` (no DTD, no entities): never ``xml.etree`` on network data.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from stockmanager.adapters.http import HttpGet
from stockmanager.news.config import TICKER_RE
from stockmanager.news.fetchers.base import USER_AGENT, NewsUnavailable, default_get, domain_of, fetch_body
from stockmanager.news.plan import NewsQuery
from stockmanager.news.raw import SNIPPET_MAX_CHARS, TITLE_MAX_CHARS, RawItem, clip, strip_html

FEED_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"


class YahooRssFetcher:
    """Per-ticker headlines; the query's ticker is used, its text is ignored."""

    name: str = "yahoo_rss"

    def __init__(
        self,
        *,
        http_get: HttpGet = default_get,
        sleep: Callable[[float], None] = time.sleep,
        throttle: Callable[[], object] = lambda: None,
    ) -> None:
        self._get = http_get
        self._sleep = sleep
        self._throttle = throttle

    def fetch(self, query: NewsQuery) -> list[RawItem]:
        """Fetch the ticker's feed. Raises ``NewsUnavailable`` on transport failure or an unparseable feed."""
        if not TICKER_RE.fullmatch(query.ticker):
            raise NewsUnavailable("bad ticker")
        url = f"{FEED_URL}?{urlencode({'s': query.ticker, 'region': 'US', 'lang': 'en-US'})}"
        headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"}
        body = fetch_body(self._get, url, headers, before_attempt=self._throttle, sleep=self._sleep)
        try:
            root = fromstring(body, forbid_dtd=True)
        except (DefusedXmlException, ParseError, ValueError) as e:
            raise NewsUnavailable(f"malformed feed: {type(e).__name__}") from e
        return [_item(node) for node in root.iterfind("./channel/item")]


def _text(node: Element, tag: str) -> str:
    """All text under ``tag``, including text after child elements that a feed failed to escape."""
    child = node.find(tag)
    return "".join(child.itertext()) if child is not None else ""


def _item(node: Element) -> RawItem:
    link = _text(node, "link").strip()
    return RawItem(
        title=clip(strip_html(_text(node, "title")), TITLE_MAX_CHARS),
        snippet=clip(strip_html(_text(node, "description")), SNIPPET_MAX_CHARS),
        url=link,
        published=_parse_pub_date(node.findtext("pubDate")),
        domain=domain_of(link),
    )


def _parse_pub_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value.strip()).date()
    except (TypeError, ValueError):
        return None
