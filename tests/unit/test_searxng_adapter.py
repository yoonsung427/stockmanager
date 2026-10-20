"""SearXNG adapter: request shape, response mapping, empty = success, failures -> NewsUnavailable."""

import json
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from stockmanager.adapters.http import HttpResponse
from stockmanager.news.fetchers.base import NewsUnavailable
from stockmanager.news.fetchers.searxng import SearxngFetcher
from tests.support.news import ScriptedGet, query


def ok(doc: Any) -> HttpResponse:
    return HttpResponse(200, json.dumps(doc).encode())


def fetcher(get: ScriptedGet) -> SearxngFetcher:
    return SearxngFetcher("http://pi.local:8080/", http_get=get, sleep=lambda _: None)


RESULT = {
    "title": "Apple beats",
    "content": "Quarterly results",
    "url": "https://www.Reuters.com/a?x=1",
    "publishedDate": "2026-10-18T14:30:00",
}


def test_request_shape() -> None:
    get = ScriptedGet(ok({"results": []}))
    fetcher(get).fetch(query("holding", "AAPL", text="AAPL stock"))
    url, headers = get.seen[0]
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("http", "pi.local:8080", "/search")
    assert parse_qs(parts.query) == {
        "q": ["AAPL stock"],
        "categories": ["news"],
        "format": ["json"],
        "time_range": ["week"],
    }
    assert "X-Finnhub-Token" not in headers


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2026, 10, 18), date(2026, 10, 19), "day"),
        (date(2026, 10, 12), date(2026, 10, 19), "week"),
        (date(2026, 10, 19), date(2026, 11, 18), "month"),
    ],
)
def test_time_range_from_window(start: date, end: date, expected: str) -> None:
    get = ScriptedGet(ok({"results": []}))
    fetcher(get).fetch(query(start=start, end=end))
    assert parse_qs(urlsplit(get.seen[0][0]).query)["time_range"] == [expected]


def test_maps_fields() -> None:
    [item] = fetcher(ScriptedGet(ok({"results": [RESULT]}))).fetch(query())
    assert (item.title, item.snippet, item.url) == ("Apple beats", "Quarterly results", "https://www.Reuters.com/a?x=1")
    assert item.published == date(2026, 10, 18)
    assert item.domain == "reuters.com"


def test_missing_or_bad_date_is_none_and_missing_fields_are_empty() -> None:
    items = fetcher(
        ScriptedGet(
            ok(
                {
                    "results": [
                        {**RESULT, "publishedDate": None},
                        {**RESULT, "publishedDate": "yesterday"},
                        {"url": "https://a.com/x"},
                        "junk",
                    ]
                }
            )
        )
    ).fetch(query())
    assert [i.published for i in items] == [None, None, None, None]
    assert (items[2].title, items[2].snippet, items[3].url) == ("", "", "")


def test_truncates_title_and_snippet() -> None:
    [item] = fetcher(ScriptedGet(ok({"results": [{**RESULT, "title": "t" * 500, "content": "c" * 5000}]}))).fetch(
        query()
    )
    assert (len(item.title), len(item.snippet)) == (300, 2000)


def test_empty_results_is_success_with_one_call() -> None:
    get = ScriptedGet(ok({"results": []}))
    assert fetcher(get).fetch(query()) == []
    assert len(get.seen) == 1


@pytest.mark.parametrize(
    "response",
    [
        HttpResponse(200, b"not json"),
        HttpResponse(200, b'{"no": "results"}'),
        HttpResponse(200, b'{"results": "x"}'),
        HttpResponse(200, b"[]"),
        HttpResponse(404, b""),
        HttpResponse(400, b""),
        HttpResponse(503, b""),
        HttpResponse(500, b""),
    ],
)
def test_failures_raise_news_unavailable(response: HttpResponse) -> None:
    with pytest.raises(NewsUnavailable):
        fetcher(ScriptedGet(response)).fetch(query())


@pytest.mark.parametrize("exc", [TimeoutError("slow"), ConnectionRefusedError("no"), OSError("net")])
def test_transport_errors_retry_once_then_raise(exc: Exception) -> None:
    get = ScriptedGet(exc)
    with pytest.raises(NewsUnavailable):
        fetcher(get).fetch(query())
    assert len(get.seen) == 2


def test_5xx_retries_once_and_can_recover() -> None:
    get = ScriptedGet(HttpResponse(503, b""), ok({"results": [RESULT]}))
    assert len(fetcher(get).fetch(query())) == 1 and len(get.seen) == 2


def test_other_4xx_is_not_retried_and_auth_is_flagged() -> None:
    get = ScriptedGet(HttpResponse(400, b""))
    with pytest.raises(NewsUnavailable):
        fetcher(get).fetch(query())
    assert len(get.seen) == 1
    with pytest.raises(NewsUnavailable) as e:
        fetcher(ScriptedGet(HttpResponse(403, b""))).fetch(query())
    assert e.value.kind == "auth"


def test_throttle_runs_before_every_request() -> None:
    calls: list[str] = []
    get = ScriptedGet(HttpResponse(503, b""), ok({"results": []}))
    f = SearxngFetcher("http://x", http_get=get, sleep=lambda _: None, throttle=lambda: calls.append("t"))
    f.fetch(query())
    assert calls == ["t", "t"]
