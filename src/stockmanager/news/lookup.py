"""Recent headlines for one ticker, on demand (summaries, not the evaluation cycle).

Unlike ``pipeline.run_news_stage`` this runs no extractor and writes nothing: it walks the configured fallback chain
for a single ``holding`` query and returns cleaned, de-duplicated, newest-first items. The text is still untrusted;
callers that hand it to a model must treat it as data (``llm.summary`` does).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, timedelta

from stockmanager.domain.ports import NewsFetcher
from stockmanager.news.config import NewsConfig
from stockmanager.news.fetchers.base import NewsUnavailable
from stockmanager.news.plan import NewsQuery
from stockmanager.news.raw import SNIPPET_MAX_CHARS, TITLE_MAX_CHARS, RawItem, clip, strip_html, url_acceptable

log = logging.getLogger(__name__)


def recent_news(
    fetchers: Sequence[NewsFetcher],
    config: NewsConfig,
    ticker: str,
    today: date,
    *,
    lookback_days: int = 3,
    limit: int = 6,
) -> list[RawItem]:
    """Up to ``limit`` items for ``ticker`` published within ``lookback_days`` of ``today`` (undated items kept).

    The first adapter in ``config.fallback_chain`` that answers with at least one usable item wins. An adapter that
    fails or comes back empty hands over to the next; if all do, the result is ``[]`` rather than an error.
    """
    since = today - timedelta(days=lookback_days)
    query = NewsQuery("holding", ticker, since, today, f"{ticker} stock")
    by_name = {f.name: f for f in fetchers}
    for name in config.fallback_chain:
        fetcher = by_name.get(name)
        if fetcher is None or "holding" not in config.sources[name].kinds:
            continue
        try:
            raw = fetcher.fetch(query)
        except NewsUnavailable as e:
            log.info("news lookup: %s unavailable for %s (%s)", name, ticker, e.kind)
            continue
        except Exception:  # an adapter bug costs one source, not the job
            log.exception("news lookup: adapter %s raised unexpectedly", name)
            continue
        items = _usable(raw, since, limit)
        if items:
            return items
    return []


def _usable(raw: Sequence[RawItem], since: date, limit: int) -> list[RawItem]:
    seen: set[str] = set()
    kept: list[RawItem] = []
    for item in raw:
        title = clip(strip_html(item.title), TITLE_MAX_CHARS)
        if not title or not url_acceptable(item.url):
            continue
        if item.published is not None and item.published < since:
            continue
        key = title.casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append(
            RawItem(title, clip(strip_html(item.snippet), SNIPPET_MAX_CHARS), item.url, item.published, item.domain)
        )
    kept.sort(key=lambda i: i.published or date.min, reverse=True)
    return kept[:limit]
