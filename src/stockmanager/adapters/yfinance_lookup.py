"""Company name -> ticker candidates via Yahoo's search endpoint (``yfinance.Search``)."""

from __future__ import annotations

import logging

import yfinance as yf

log = logging.getLogger(__name__)


def search_symbols(name: str) -> list[str]:
    """Upper-cased ticker symbols Yahoo suggests for ``name``, best match first; ``[]`` on any failure."""
    try:
        quotes = yf.Search(name, max_results=6, news_count=0, lists_count=0, timeout=10).quotes
    except Exception as e:  # noqa: BLE001 - yfinance raises a wide variety of errors; an alias miss is no crash
        log.info("symbol search failed for %r: %s", name, type(e).__name__)
        return []
    return [str(q["symbol"]).upper() for q in quotes if q.get("symbol")]
