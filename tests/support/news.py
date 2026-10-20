"""Test helpers for the news pipeline: raw items, prepared items, fake fetchers and a fake extractor."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

from stockmanager.adapters.http import HttpResponse
from stockmanager.domain.docs import NewsEvent
from stockmanager.domain.ports import NewsFetcher
from stockmanager.news.extract import Candidate, ExtractContext
from stockmanager.news.fetchers.base import NewsUnavailable
from stockmanager.news.normalise import PreparedItem
from stockmanager.news.plan import NewsQuery
from stockmanager.news.raw import RawItem
from tests.support.books import T0, make_portfolio, make_trade

TODAY = date(2026, 10, 19)
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=T0.tzinfo)


def clock() -> datetime:
    """Fixed PT clock."""
    return NOW


def raw(
    title: str = "Acme beats estimates",
    url: str = "https://news.example.com/a",
    snippet: str = "snippet",
    published: date | None = TODAY,
    domain: str | None = None,
) -> RawItem:
    """A RawItem; the domain defaults to the URL's host."""
    host = url.split("/")[2].removeprefix("www.") if "//" in url else ""
    return RawItem(title, snippet, url, published, host if domain is None else domain)


def prepared(item: RawItem | None = None, *, item_id: str = "i1", focus: str | None = "AAPL") -> PreparedItem:
    """A PreparedItem around ``item``."""
    return PreparedItem(item or raw(), item_id, 0, focus, "searxng")


def candidate(
    ticker: str = "AAPL",
    event: NewsEvent = "earnings",
    description: str = "Apple reported results.",
    when: date = TODAY,
) -> Candidate:
    """A Candidate."""
    return Candidate(ticker=ticker, event=event, description=description, date=when)


def query(kind: Any = "holding", ticker: str = "AAPL", **kw: Any) -> NewsQuery:
    """A NewsQuery with a one-week window ending TODAY."""
    return NewsQuery(
        kind, ticker, kw.get("start", date(2026, 10, 12)), kw.get("end", TODAY), kw.get("text", f"{ticker} stock")
    )


def portfolio(*tickers: str) -> Any:
    """A portfolio holding one lot of each ticker."""
    return make_portfolio([make_trade(i + 1, ticker=t) for i, t in enumerate(tickers)], start_cash="100000")


class FakeFetcher:
    """NewsFetcher whose answer per query is a list of items, an exception, or a callable."""

    def __init__(self, name: str, answer: Any = None) -> None:
        self.name = name
        self.answer: Any = [] if answer is None else answer
        self.calls: list[NewsQuery] = []
        self._lock = threading.Lock()

    def fetch(self, query: NewsQuery) -> list[RawItem]:
        with self._lock:
            self.calls.append(query)
        answer = self.answer(query) if callable(self.answer) else self.answer
        if isinstance(answer, Exception):
            raise answer
        return list(answer)


def failing(name: str, kind: str = "error") -> FakeFetcher:
    """A fetcher whose every call raises NewsUnavailable."""
    return FakeFetcher(name, NewsUnavailable("down", kind=kind))  # type: ignore[arg-type]


class FakeExtractor:
    """Extractor returning ``respond(item)`` (a list of candidates or an ExtractFailure) and recording each call."""

    def __init__(self, respond: Callable[[PreparedItem], Any] | None = None) -> None:
        self.respond = respond or (lambda item: [])
        self.items: list[PreparedItem] = []
        self.contexts: list[ExtractContext] = []
        self._lock = threading.Lock()

    def verify(self) -> None:
        return None

    def extract(self, item: PreparedItem, ctx: ExtractContext) -> list[Candidate]:
        with self._lock:
            self.items.append(item)
            self.contexts.append(ctx)
        out = self.respond(item)
        if isinstance(out, Exception):
            raise out
        return list(out)


def as_fetchers(*fetchers: FakeFetcher) -> list[NewsFetcher]:
    """Typed view of fakes."""
    return list(fetchers)


class ScriptedGet:
    """HttpGet fake: pops one scripted response (or exception) per call and records (url, headers)."""

    def __init__(self, *script: HttpResponse | Exception) -> None:
        self.script = list(script)
        self.seen: list[tuple[str, Mapping[str, str]]] = []

    def __call__(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        self.seen.append((url, headers))
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        return step
