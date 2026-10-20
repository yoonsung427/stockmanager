"""Normalise: truncate, HTML strip, URL checks, recency, URL/title dedupe, per-query cap, round-robin (news-sources §3.2-3.3)."""

from datetime import timedelta

import pytest

from stockmanager.news.config import NewsConfig
from stockmanager.news.normalise import QueryResult, normalise, normalise_url, title_key
from stockmanager.news.raw import RawItem
from tests.support.news import TODAY, query, raw

CFG = NewsConfig()


def one(*items: RawItem, kind: str = "holding", cfg: NewsConfig = CFG) -> tuple[list[RawItem], dict[str, int]]:
    out = normalise([QueryResult(0, query(kind), "searxng", items)], cfg, TODAY)
    reasons: dict[str, int] = {}
    for r in out.rejections:
        reasons[r.reason] = reasons.get(r.reason, 0) + 1
    return [p.item for p in out.items], reasons


def test_truncates_and_strips_html() -> None:
    kept, _ = one(
        raw(title="<b>Acme</b> &amp; Co " + "x" * 400, snippet="<p>hello&nbsp;<a href='x'>world</a></p>" + "y" * 3000)
    )
    assert kept[0].title.startswith("Acme & Co ") and len(kept[0].title) == 300
    assert kept[0].snippet.startswith("hello world") and len(kept[0].snippet) == 2000


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://x.com/a",
        "javascript:alert(1)",
        "https://user:pw@x.com/a",
        "https://x.com/" + "a" * 2050,
        "https:///nohost",
        "https://x.com:99999/",
    ],
)
def test_bad_urls_rejected(url: str) -> None:
    kept, reasons = one(raw(url=url))
    assert kept == [] and reasons == {"bad_url": 1}


def test_empty_title_is_bad_item() -> None:
    assert one(raw(title="<br>"))[1] == {"bad_item": 1}


def test_recency() -> None:
    old = raw(url="https://a.com/old", published=TODAY - timedelta(days=8), title="old")
    edge = raw(url="https://a.com/edge", published=TODAY - timedelta(days=7), title="edge")
    future = raw(url="https://a.com/fut", published=TODAY + timedelta(days=2), title="future")
    tomorrow = raw(url="https://a.com/tom", published=TODAY + timedelta(days=1), title="tomorrow")
    undated = raw(url="https://a.com/und", published=None, title="undated")
    kept, reasons = one(old, edge, future, tomorrow, undated)
    assert {i.title for i in kept} == {"edge", "tomorrow", "undated"}
    assert reasons == {"stale": 1, "bad_date": 1}


def test_earnings_items_exempt_from_lookback_floor() -> None:
    old = raw(published=TODAY - timedelta(days=30))
    assert len(one(old, kind="earnings")[0]) == 1
    assert one(old, kind="holding")[1] == {"stale": 1}


def test_url_normalisation() -> None:
    a = "HTTPS://www.Example.com:443/Path/?b=2&utm_source=x&a=1&fbclid=z#frag"
    assert normalise_url(a) == "https://example.com/Path?a=1&b=2"
    assert normalise_url("http://example.com:80/") == "http://example.com/"
    assert normalise_url("http://example.com:8080/x/") == "http://example.com:8080/x"


def test_url_dedupe_across_tracking_www_fragment() -> None:
    a = raw(url="https://www.example.com/a?utm_medium=x#top", title="First")
    b = raw(url="https://example.com/a/", title="Second")
    kept, reasons = one(a, b)
    assert [i.title for i in kept] == ["First"] and reasons == {"dup_url": 1}


def test_url_dedupe_is_global_across_queries_and_first_query_owns_focus() -> None:
    item = raw(url="https://a.com/x")
    out = normalise(
        [
            QueryResult(0, query("holding", "AAPL"), "searxng", [item]),
            QueryResult(1, query("sector", "XLK"), "searxng", [item]),
        ],
        CFG,
        TODAY,
    )
    assert len(out.items) == 1 and out.items[0].focus_ticker == "AAPL" and out.items[0].query_index == 0


def test_benchmark_sector_macro_have_no_focus_ticker() -> None:
    for kind in ("benchmark", "sector", "macro"):
        out = normalise([QueryResult(0, query(kind, "SPY"), "searxng", [raw()])], CFG, TODAY)
        assert out.items[0].focus_ticker is None


def test_title_key_ignores_case_punctuation_and_width() -> None:
    assert title_key("Acme  BEATS estimates!") == title_key("acme beats, estimates")
    assert title_key("ＡＣＭＥ beats") == title_key("acme beats")
    assert title_key("!!!") == ""


def test_syndicated_title_keeps_earliest_published() -> None:
    late = raw(url="https://a.com/1", title="Acme beats estimates", published=TODAY)
    early = raw(url="https://b.com/2", title="ACME beats estimates!", published=TODAY - timedelta(days=2))
    kept, reasons = one(late, early)
    assert [i.url for i in kept] == ["https://b.com/2"] and reasons == {"dup_title": 1}


def test_title_dedupe_tie_prefers_higher_priority_adapter() -> None:
    a = raw(url="https://a.com/1", title="Same headline")
    b = raw(url="https://b.com/2", title="Same headline")
    out = normalise(
        [QueryResult(0, query(), "yahoo_rss", [a]), QueryResult(1, query(), "searxng", [b])],
        CFG,
        TODAY,
        {"searxng": 0, "yahoo_rss": 2},
    )
    assert [p.item.url for p in out.items] == ["https://b.com/2"]


def test_per_query_cap_keeps_newest() -> None:
    cfg = NewsConfig(max_items_per_query=2)
    items = [raw(url=f"https://a.com/{d}", title=f"t{d}", published=TODAY - timedelta(days=d)) for d in (3, 0, 2, 1)]
    kept, reasons = one(*items, cfg=cfg)
    assert [i.title for i in kept] == ["t0", "t1"] and reasons == {"cap_query": 2}


def test_item_id_is_hash_of_normalised_url_only() -> None:
    import hashlib

    out = normalise(
        [QueryResult(0, query(), "searxng", [raw(url="https://www.a.com/x?utm_x=1", title="secret words")])], CFG, TODAY
    )
    assert out.items[0].item_id == hashlib.sha256(b"https://a.com/x").hexdigest()


def test_round_robin_keeps_every_query_represented() -> None:
    cfg = NewsConfig(max_items_per_cycle=10, max_items_per_query=10)
    results = [
        QueryResult(
            i,
            query("holding", f"T{i}"),
            "searxng",
            [raw(url=f"https://a.com/{i}/{n}", title=f"q{i} n{n}") for n in range(10)],
        )
        for i in range(25)
    ]
    out = normalise(results, cfg, TODAY)
    assert len(out.items) == 10
    assert {p.query_index for p in out.items} == set(range(10))  # one from each of the first 10 queries
    assert sum(r.reason == "cap_items" for r in out.rejections) == 240


def test_round_robin_second_round_goes_back_to_first_query() -> None:
    cfg = NewsConfig(max_items_per_cycle=3, max_items_per_query=2)
    results = [
        QueryResult(
            0, query(), "searxng", [raw(url="https://a.com/a1", title="a1"), raw(url="https://a.com/a2", title="a2")]
        ),
        QueryResult(1, query(), "searxng", [raw(url="https://a.com/b1", title="b1")]),
    ]
    assert [p.item.title for p in normalise(results, cfg, TODAY).items] == ["a1", "b1", "a2"]


def test_rejections_carry_no_text() -> None:
    out = normalise(
        [QueryResult(0, query(), "searxng", [raw(url="https://u:p@x.com/", title="TOP SECRET HEADLINE")])], CFG, TODAY
    )
    assert "SECRET" not in repr(out.rejections)
