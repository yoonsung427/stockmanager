"""Finnhub news adapter: company-news / general / earnings calendar, redirect domains, 429 retry, memoisation."""

import json
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from stockmanager.adapters.http import HttpResponse
from stockmanager.news.fetchers.base import NewsUnavailable
from stockmanager.news.fetchers.finnhub import FinnhubNewsFetcher
from tests.support.news import ScriptedGet, query

TS = int(datetime(2026, 10, 18, 12, 0, tzinfo=UTC).timestamp())
ARTICLE = {
    "headline": "Apple beats",
    "summary": "Results",
    "url": "https://www.cnbc.com/a",
    "datetime": TS,
    "source": "CNBC",
}


def ok(doc: Any) -> HttpResponse:
    return HttpResponse(200, json.dumps(doc).encode())


def make(get: ScriptedGet, sleeps: list[float] | None = None, key: str = "sekret") -> FinnhubNewsFetcher:
    return FinnhubNewsFetcher(key, http_get=get, sleep=(sleeps.append if sleeps is not None else lambda _: None))


def test_company_news_request_and_mapping() -> None:
    get = ScriptedGet(ok([ARTICLE]))
    [item] = make(get).fetch(query("holding", "AAPL"))
    url, headers = get.seen[0]
    parts = urlsplit(url)
    assert (parts.netloc, parts.path) == ("finnhub.io", "/api/v1/company-news")
    assert parse_qs(parts.query) == {"symbol": ["AAPL"], "from": ["2026-10-12"], "to": ["2026-10-19"]}
    assert headers["X-Finnhub-Token"] == "sekret" and "sekret" not in url  # header only, never the URL
    assert (item.title, item.snippet, item.url, item.domain) == (
        "Apple beats",
        "Results",
        "https://www.cnbc.com/a",
        "cnbc.com",
    )
    assert item.published == date(2026, 10, 18)


@pytest.mark.parametrize(
    ("source", "domain"),
    [
        ("Seeking Alpha", "seeking-alpha"),
        ("Yahoo", "yahoo"),
        ("MarketWatch.com", "marketwatch.com"),
        ("", "finnhub.io"),
        ("!!!", "finnhub.io"),
        ("x" * 80, "finnhub.io"),
    ],
)
def test_finnhub_redirect_url_takes_slugified_source_as_domain(source: str, domain: str) -> None:
    redirect = {**ARTICLE, "url": "https://finnhub.io/api/news?id=abc", "source": source}
    [item] = make(ScriptedGet(ok([redirect]))).fetch(query())
    assert item.domain == domain and item.url == "https://finnhub.io/api/news?id=abc"  # URL kept as given


def test_missing_source_on_redirect_and_missing_fields() -> None:
    items = make(
        ScriptedGet(ok([{"url": "https://finnhub.io/r/1"}, {"headline": "x"}, "junk", {**ARTICLE, "datetime": "bad"}]))
    ).fetch(query())
    assert items[0].domain == "finnhub.io"
    assert (items[1].url, items[1].published) == ("", None)
    assert items[3].published is None


def test_429_honours_retry_after_then_retries_once() -> None:
    sleeps: list[float] = []
    get = ScriptedGet(HttpResponse(429, b"", {"retry-after": "7"}), ok([ARTICLE]))
    assert len(make(get, sleeps).fetch(query())) == 1
    assert sleeps == [7.0] and len(get.seen) == 2


def test_429_without_retry_after_waits_ten_seconds() -> None:
    sleeps: list[float] = []
    make(ScriptedGet(HttpResponse(429, b""), ok([])), sleeps).fetch(query())
    assert sleeps == [10.0]


def test_429_twice_fails_after_one_retry() -> None:
    get = ScriptedGet(HttpResponse(429, b"", {"retry-after": "1"}))
    with pytest.raises(NewsUnavailable):
        make(get).fetch(query())
    assert len(get.seen) == 2


def test_retry_after_is_capped() -> None:
    sleeps: list[float] = []
    make(ScriptedGet(HttpResponse(429, b"", {"retry-after": "9999"}), ok([])), sleeps).fetch(query())
    assert sleeps == [30.0]


def test_auth_failure_and_missing_key() -> None:
    with pytest.raises(NewsUnavailable) as e:
        make(ScriptedGet(HttpResponse(401, b""))).fetch(query())
    assert e.value.kind == "auth"
    get = ScriptedGet(ok([]))
    with pytest.raises(NewsUnavailable) as e2:
        make(get, key="").fetch(query())
    assert e2.value.kind == "auth" and get.seen == []


def test_key_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINNHUB_API_KEY", "from-env")
    get = ScriptedGet(ok([]))
    FinnhubNewsFetcher(http_get=get).fetch(query())
    assert get.seen[0][1]["X-Finnhub-Token"] == "from-env"


@pytest.mark.parametrize("body", [b"nope", b'{"error": "x"}', b'"str"'])
def test_malformed_news_body(body: bytes) -> None:
    with pytest.raises(NewsUnavailable):
        make(ScriptedGet(HttpResponse(200, body))).fetch(query())


def test_empty_list_is_success() -> None:
    assert make(ScriptedGet(ok([]))).fetch(query()) == []


def test_macro_uses_general_news() -> None:
    get = ScriptedGet(ok([ARTICLE]))
    make(get).fetch(query("macro", "SPY"))
    parts = urlsplit(get.seen[0][0])
    assert parts.path == "/api/v1/news" and parse_qs(parts.query) == {"category": ["general"]}


def test_earnings_calendar_synthesises_one_item_per_row() -> None:
    doc = {
        "earningsCalendar": [
            {"symbol": "AAPL", "date": "2026-10-29", "hour": "amc"},
            {"symbol": "AAPL", "date": "bad"},
            {"symbol": "MSFT", "date": "2026-10-30"},
            {"symbol": "a; drop", "date": "2026-10-30"},
        ]
    }
    get = ScriptedGet(ok(doc))
    items = make(get).fetch(query("earnings", "AAPL", start=date(2026, 10, 19), end=date(2026, 11, 18)))
    parts = urlsplit(get.seen[0][0])
    assert parts.path == "/api/v1/calendar/earnings"
    assert parse_qs(parts.query) == {"from": ["2026-10-19"], "to": ["2026-11-18"], "symbol": ["AAPL"]}
    [item] = items  # bad date, other symbol and bad symbol are skipped
    assert item.title == "AAPL earnings scheduled 2026-10-29" and item.snippet == "" and item.published is None
    assert item.domain == "finnhub.io" and item.url == get.seen[0][0]


def test_earnings_calendar_malformed() -> None:
    with pytest.raises(NewsUnavailable):
        make(ScriptedGet(ok([]))).fetch(query("earnings"))


def test_holding_and_analyst_share_one_request() -> None:
    get = ScriptedGet(ok([ARTICLE]))
    f = make(get)
    a = f.fetch(query("holding", "AAPL"))
    b = f.fetch(query("analyst", "AAPL", text="AAPL analyst upgrade"))
    assert a == b and len(get.seen) == 1
    f.fetch(query("holding", "MSFT"))
    assert len(get.seen) == 2


def test_failures_are_not_memoised() -> None:
    get = ScriptedGet(HttpResponse(400, b""), ok([]))
    f = make(get)
    with pytest.raises(NewsUnavailable):
        f.fetch(query())
    assert f.fetch(query()) == []


def test_throttle_counts_real_requests_only() -> None:
    calls: list[int] = []
    f = FinnhubNewsFetcher("k", http_get=ScriptedGet(ok([])), sleep=lambda _: None, throttle=lambda: calls.append(1))
    f.fetch(query("holding", "AAPL"))
    f.fetch(query("analyst", "AAPL"))
    assert len(calls) == 1
