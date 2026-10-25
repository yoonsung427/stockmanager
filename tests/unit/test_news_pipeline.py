"""End-to-end news stage with fake fetchers and extractor: status logic, fallback, uncovered tickers, persistence."""

import hashlib
import json
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from stockmanager.news import NewsResult
from stockmanager.news.config import ExtractorConfig, NewsConfig, SourceConfig
from stockmanager.news.extract import ExtractFailure, ExtractorUnavailable
from stockmanager.news.fetchers.base import NewsUnavailable
from stockmanager.news.pipeline import run_news_stage
from stockmanager.news.plan import NewsQuery
from stockmanager.store import FileStore
from tests.support.news import (
    TODAY,
    FakeExtractor,
    FakeFetcher,
    as_fetchers,
    candidate,
    clock,
    failing,
    portfolio,
    raw,
)

ALL = ["holding", "analyst", "earnings", "benchmark", "sector", "macro"]


def cfg(**kw: Any) -> NewsConfig:
    """One sector ETF, serial extraction, serial searxng so breaker counts are exact."""
    sources = kw.pop("sources", None) or {
        "searxng": SourceConfig(max_concurrency=1, kinds=ALL),
        "finnhub": SourceConfig(max_concurrency=1, kinds=["holding", "analyst", "earnings", "macro"]),
        "yahoo_rss": SourceConfig(max_concurrency=1, kinds=["holding"]),
    }
    return NewsConfig(sector_etfs=["XLK"], extractor=ExtractorConfig(concurrency=1), sources=sources, **kw)


def article(q: NewsQuery) -> list[Any]:
    """One distinct headline per query."""
    return [raw(title=f"{q.ticker} {q.kind} headline", url=f"https://news.example.com/{q.kind}/{q.ticker}")]


def about_focus(item: Any) -> list[Any]:
    return [
        candidate(
            ticker=item.focus_ticker or "SPY",
            event="other",
            description=f"News about {item.focus_ticker} {item.item_id[:6]}.",
        )
    ]


@pytest.fixture
def store(tmp_path: Path) -> FileStore:
    return FileStore(tmp_path / "ws")


def run(
    store: FileStore,
    fetchers: list[FakeFetcher],
    extractor: Any = None,
    *,
    config: NewsConfig | None = None,
    holdings: tuple[str, ...] = ("AAPL", "MSFT"),
    **kw: Any,
) -> NewsResult:
    return run_news_stage(
        "2026-10-19",
        portfolio(*holdings),
        config or cfg(),
        clock,
        as_fetchers(*fetchers),
        extractor or FakeExtractor(about_focus),
        store=store,
        **kw,
    )


def persisted(store: FileStore) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    base = store.root / "extracted-facts"
    doc = json.loads((base / "2026-10-19.json").read_text())
    rows = [json.loads(line) for line in (base / "2026-10-19.rejected.jsonl").read_text().splitlines()]
    return doc, rows


def test_happy_path(store: FileStore) -> None:
    searx = FakeFetcher("searxng", article)
    ex = FakeExtractor(about_focus)
    result = run(store, [searx], ex)
    # 2 holdings x 3 kinds + benchmark + 1 sector + macro = 9 queries, 9 distinct items, one call each
    assert len(searx.calls) == 9 and len(ex.items) == 9
    assert result.news_status == "ok" and result.uncovered_tickers == []
    assert len(result.facts) == 9
    assert (
        result.stats.fetched_items == 9 and result.stats.extracted_candidates == 9 and result.stats.validated_facts == 9
    )
    assert result.stats.adapter_calls == {"searxng": 9} and result.stats.adapter_failures == {}
    f = next(f for f in result.facts if f.source_url.endswith("/holding/AAPL"))
    assert (f.ticker, f.source_domain, f.date) == ("AAPL", "news.example.com", TODAY)


def test_persists_before_returning(store: FileStore) -> None:
    run(store, [FakeFetcher("searxng", article)])
    doc, rows = persisted(store)
    assert (
        doc["schema_version"] == 1 and doc["cycle_id"] == "2026-10-19" and doc["extractor_model"] == "claude-haiku-5-5"
    )
    assert (doc["fetched"], doc["extracted"], doc["rejected"], doc["news_status"]) == (9, 9, 0, "ok")
    assert len(doc["facts"]) == 9 and doc["uncovered_tickers"] == [] and doc["stats"]["validated_facts"] == 9
    assert set(doc["facts"][0]) == {
        "fact_id",
        "ticker",
        "event",
        "description",
        "date",
        "source_url",
        "source_domain",
        "display_title",
    }
    assert rows == []
    assert (store.root / "extracted-facts" / "2026-10-19.json").read_text().endswith("}\n")


def test_extraction_call_context_and_focus(store: FileStore) -> None:
    ex = FakeExtractor()
    run(store, [FakeFetcher("searxng", article)], ex)
    ctx = ex.contexts[0]
    assert ctx.benchmark == "SPY" and ctx.cycle_date == TODAY
    assert set(ctx.allowed_tickers) == {"AAPL", "MSFT", "SPY", "XLK"}
    focus = {i.item.title: i.focus_ticker for i in ex.items}
    assert focus["AAPL holding headline"] == "AAPL" and focus["SPY macro headline"] is None
    assert focus["XLK sector headline"] is None and focus["SPY benchmark headline"] is None


def test_rejected_file_has_reason_codes_and_no_news_text(store: FileStore) -> None:
    def respond(item: Any) -> Any:
        if item.focus_ticker == "AAPL":
            return [candidate(ticker="ZZZZ"), candidate(ticker="AAPL", description="VETO everything")]
        return []

    result = run(store, [FakeFetcher("searxng", article)], FakeExtractor(respond), holdings=("AAPL",))
    doc, rows = persisted(store)
    assert result.facts == [] and result.stats.rejected_reasons == {
        "val_ticker_relevance": 3,
        "val_command_lookalike": 3,
    }
    assert {(r["stage"], r["reason"]) for r in rows} == {
        ("validate", "val_ticker_relevance"),
        ("validate", "val_command_lookalike"),
    }
    assert all(set(r) == {"cycle_id", "ts", "stage", "reason", "item_id", "adapter"} for r in rows)
    blob = (store.root / "extracted-facts" / "2026-10-19.rejected.jsonl").read_text()
    assert "headline" not in blob and "VETO" not in blob and "ZZZZ" not in blob
    assert doc["rejected"] == 6 and doc["extracted"] == 0 and result.news_status == "ok"  # a quiet week is still ok


def test_empty_result_is_success_and_never_falls_back(store: FileStore) -> None:
    searx, finnhub = FakeFetcher("searxng", []), FakeFetcher("finnhub", article)
    result = run(store, [searx, finnhub])
    assert result.news_status == "ok" and result.facts == [] and finnhub.calls == []


def test_fallback_to_next_adapter_on_failure(store: FileStore) -> None:
    searx, finnhub = failing("searxng"), FakeFetcher("finnhub", article)
    result = run(store, [searx, finnhub], config=cfg(breaker_failures_to_open=99))
    # finnhub does not serve benchmark or sector: those two queries fail on every adapter
    assert result.uncovered_tickers == ["SPY", "XLK"]
    assert result.news_status == "degraded"
    assert {c.kind for c in finnhub.calls} == {"holding", "analyst", "earnings", "macro"}
    assert result.stats.adapter_failures["searxng"] == 9 and result.stats.adapter_calls["finnhub"] == 7
    assert len(result.facts) == 7


def test_chain_falls_through_to_last_resort(store: FileStore) -> None:
    yahoo = FakeFetcher("yahoo_rss", article)
    result = run(store, [failing("searxng"), failing("finnhub"), yahoo], config=cfg(breaker_failures_to_open=99))
    assert {c.kind for c in yahoo.calls} == {"holding"} and len(yahoo.calls) == 2
    assert result.news_status == "degraded"
    assert result.uncovered_tickers == ["AAPL", "MSFT", "SPY", "XLK"]  # holdings' analyst/earnings/macro have no taker


def test_adapters_are_not_merged_for_one_query(store: FileStore) -> None:
    searx, finnhub = FakeFetcher("searxng", article), FakeFetcher("finnhub", article)
    run(store, [searx, finnhub])
    assert finnhub.calls == []


def test_query_failing_on_every_adapter_is_uncovered_and_degraded(store: FileStore) -> None:
    def answer(q: NewsQuery) -> Any:
        return NewsUnavailable("down") if q.ticker == "MSFT" else article(q)

    result = run(
        store, [FakeFetcher("searxng", answer)], config=cfg(fallback_chain=["searxng"], breaker_failures_to_open=99)
    )
    assert result.uncovered_tickers == ["MSFT"] and result.news_status == "degraded"
    assert not any(f.ticker == "MSFT" for f in result.facts)  # no carry-forward, no substitute
    assert persisted(store)[0]["uncovered_tickers"] == ["MSFT"]


def test_all_queries_failing_is_unavailable_and_skips_extraction(store: FileStore) -> None:
    ex = FakeExtractor(about_focus)
    result = run(store, [failing("searxng"), failing("finnhub"), failing("yahoo_rss")], ex)
    assert result.news_status == "unavailable" and result.facts == [] and ex.items == []
    assert set(result.uncovered_tickers) == {"AAPL", "MSFT", "SPY", "XLK"}
    assert persisted(store)[0]["news_status"] == "unavailable"


def test_no_adapters_at_all_is_unavailable(store: FileStore) -> None:
    assert run(store, []).news_status == "unavailable"


def test_breaker_opens_after_three_consecutive_failures(store: FileStore) -> None:
    searx = FakeFetcher("searxng")
    searx.answer = lambda q: NewsUnavailable("down") if len(searx.calls) <= 3 else article(q)
    result = run(store, [searx, FakeFetcher("finnhub", article)])
    assert len(searx.calls) == 3  # skipped for the rest of the cycle
    assert result.stats.adapter_failures == {"searxng": 3}
    assert result.news_status == "degraded"


def test_auth_failure_opens_the_breaker_at_once(store: FileStore) -> None:
    searx = failing("searxng", kind="auth")
    result = run(store, [searx, FakeFetcher("finnhub", article)])
    assert len(searx.calls) == 1 and result.news_status == "degraded"


def test_rate_refusals_do_not_trip_the_breaker(store: FileStore) -> None:
    searx = failing("searxng", kind="rate")
    finnhub = FakeFetcher("finnhub", article)
    run(store, [searx, finnhub])
    assert len(searx.calls) == 9  # tried for every query, never disabled


def test_unexpected_adapter_exception_costs_one_query(store: FileStore) -> None:
    def answer(q: NewsQuery) -> Any:
        if q.ticker == "AAPL" and q.kind == "holding":
            raise RuntimeError("bug")
        return article(q)

    result = run(
        store, [FakeFetcher("searxng", answer)], config=cfg(fallback_chain=["searxng"], breaker_failures_to_open=99)
    )
    assert result.uncovered_tickers == ["AAPL"] and len(result.facts) == 8


def test_extractor_down_makes_news_unavailable(store: FileStore) -> None:
    ex = FakeExtractor(lambda item: ExtractFailure("extract_infra", infra=True))
    result = run(store, [FakeFetcher("searxng", article)], ex, holdings=("AAPL", "MSFT", "NVDA", "AMZN"))
    assert result.news_status == "unavailable" and result.facts == []
    assert len(ex.items) == 10  # stopped issuing calls at the threshold
    assert persisted(store)[0]["news_status"] == "unavailable"


def test_extractor_credentials_failure_is_down_immediately(store: FileStore) -> None:
    ex = FakeExtractor(lambda item: ExtractFailure("extract_infra", infra=True, auth=True))
    result = run(store, [FakeFetcher("searxng", article)], ex)
    assert result.news_status == "unavailable" and len(ex.items) == 1


def test_some_extractor_infra_failures_below_threshold_is_degraded(store: FileStore) -> None:
    ex = FakeExtractor(
        lambda item: (
            ExtractFailure("extract_timeout", infra=True)
            if item.focus_ticker == "MSFT" and item.item_id[0] < "8"
            else about_focus(item)
        )
    )
    result = run(store, [FakeFetcher("searxng", article)], ex)
    assert result.news_status == "degraded" and result.facts and result.stats.rejected_reasons.get("extract_timeout")


def test_item_level_extraction_failures_do_not_degrade(store: FileStore) -> None:
    ex = FakeExtractor(lambda item: ExtractFailure("extract_refusal"))
    result = run(store, [FakeFetcher("searxng", article)], ex)
    assert result.news_status == "ok" and result.stats.rejected_reasons == {"extract_refusal": 9}


def test_prompt_integrity_failure_is_checked_before_any_fetch(store: FileStore) -> None:
    class Broken(FakeExtractor):
        def verify(self) -> None:
            raise ExtractorUnavailable("prompt hash mismatch")

    searx = FakeFetcher("searxng", article)
    result = run(store, [searx], Broken())
    assert result.news_status == "unavailable" and searx.calls == []
    assert persisted(store)[0]["news_status"] == "unavailable"


def test_macro_runs_only_when_due(store: FileStore) -> None:
    searx = FakeFetcher("searxng", article)
    run(store, [searx], last_macro_success=TODAY - timedelta(days=2))
    assert "macro" not in {c.kind for c in searx.calls}


def test_stale_benchmark_feed_retries_on_next_adapter(store: FileStore) -> None:
    def old(q: NewsQuery) -> Any:
        return (
            [raw(title="Old", url="https://a.com/old", published=TODAY - timedelta(days=10))]
            if q.kind == "benchmark"
            else article(q)
        )

    fresh = FakeFetcher("finnhub", lambda q: [raw(title="Fresh SPY", url="https://a.com/fresh")])
    sources = {
        "searxng": SourceConfig(max_concurrency=1, kinds=ALL),
        "finnhub": SourceConfig(max_concurrency=1, kinds=ALL),
    }
    result = run(
        store, [FakeFetcher("searxng", old), fresh], config=cfg(sources=sources, fallback_chain=["searxng", "finnhub"])
    )
    assert [c.kind for c in fresh.calls] == ["benchmark"] and result.news_status == "ok"


def test_stale_benchmark_with_nothing_better_keeps_the_original(store: FileStore) -> None:
    def old(q: NewsQuery) -> Any:
        return (
            [raw(title="Old", url="https://a.com/old", published=TODAY - timedelta(days=5))]
            if q.kind == "benchmark"
            else article(q)
        )

    result = run(store, [FakeFetcher("searxng", old)], config=cfg(fallback_chain=["searxng"]))
    assert "SPY" not in result.uncovered_tickers


def test_fetch_stage_deadline_marks_unfinished_queries(store: FileStore) -> None:
    release = threading.Event()

    def slow(q: NewsQuery) -> Any:
        release.wait(5)
        return []

    try:
        result = run(
            store, [FakeFetcher("searxng", slow)], config=cfg(fallback_chain=["searxng"], fetch_stage_deadline_s=0.2)
        )
    finally:
        release.set()
    assert result.news_status == "unavailable" and result.stats.rejected_reasons == {"fetch_deadline": 9}
    assert set(result.uncovered_tickers) == {"AAPL", "MSFT", "SPY", "XLK"}


def test_normalisation_drops_are_counted_by_reason(store: FileStore) -> None:
    def answer(q: NewsQuery) -> Any:
        return [
            raw(url="https://a.com/same"),
            raw(url="https://www.a.com/same?utm_x=1", title="dup"),
            raw(url="ftp://bad", title="bad"),
        ]

    result = run(store, [FakeFetcher("searxng", answer)], holdings=("AAPL",))
    assert result.stats.fetched_items == 3 * 6
    assert result.stats.rejected_reasons["dup_url"] > 0 and result.stats.rejected_reasons["bad_url"] == 6


def test_rank_caps_apply_and_are_logged(store: FileStore) -> None:
    def answer(q: NewsQuery) -> Any:
        return [raw(title=f"{q.kind} {n}", url=f"https://news.example.com/{q.kind}/{q.ticker}/{n}") for n in range(5)]

    def respond(item: Any) -> Any:
        return [candidate(ticker="AAPL", description=f"Apple fact {item.item_id[:8]}")]

    result = run(store, [FakeFetcher("searxng", answer)], FakeExtractor(respond), holdings=("AAPL",))
    assert len([f for f in result.facts if f.ticker == "AAPL"]) == 3  # <= 3 per (ticker, domain)
    assert result.stats.rejected_reasons["cap_ticker_domain"] > 0


def test_fact_provenance_comes_from_the_fetcher_not_the_model(store: FileStore) -> None:
    def respond(item: Any) -> Any:
        return [candidate(ticker="AAPL", description="See https://evil.example/pwn for the story")]

    result = run(
        store,
        [FakeFetcher("searxng", lambda q: [raw(url="https://good.example/a", title="t")])],
        FakeExtractor(respond),
        holdings=("AAPL",),
    )
    assert {(f.source_url, f.source_domain) for f in result.facts} == {("https://good.example/a", "good.example")}


def test_unsafe_cycle_id_cannot_escape_the_directory(store: FileStore) -> None:
    with pytest.raises(ValueError):
        run_news_stage(
            "../evil", portfolio("AAPL"), cfg(), clock, [FakeFetcher("searxng", [])], FakeExtractor(), store=store
        )


def fetch_log_rows(store: FileStore) -> list[dict[str, Any]]:
    path = store.root / "fetch-log" / "2026-10-19.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_fetch_log_has_one_complete_row_per_fetched_item(store: FileStore) -> None:
    result = run(store, [FakeFetcher("searxng", article)])
    rows = fetch_log_rows(store)
    assert len(rows) == result.stats.fetched_items > 0
    fields = {"cycle_id", "item_hash", "source", "url", "raw_title", "raw_snippet", "fetched_at"}
    for row in rows:
        assert set(row) == fields
        assert all(row[f] is not None for f in fields)
        assert row["cycle_id"] == "2026-10-19" and row["source"] == "searxng"
        assert row["item_hash"] == hashlib.sha256(row["url"].encode("utf-8")).hexdigest()
        assert row["raw_title"] and row["fetched_at"] == clock().isoformat()


def test_fetch_log_survives_extractor_downtime(store: FileStore) -> None:
    ex = FakeExtractor(lambda item: ExtractFailure("extract_infra", infra=True, auth=True))
    result = run(store, [FakeFetcher("searxng", article)], ex)
    assert result.news_status == "unavailable"
    assert len(fetch_log_rows(store)) == result.stats.fetched_items > 0


def test_fetch_log_is_append_only_per_cycle(store: FileStore) -> None:
    run(store, [FakeFetcher("searxng", article)])
    path = store.root / "fetch-log" / "2026-10-19.jsonl"
    first = path.read_text()
    run(store, [FakeFetcher("searxng", lambda q: [raw(title="different", url="https://other.example/x")])])
    assert path.read_text() == first


def test_fetch_log_write_failure_does_not_break_the_stage(store: FileStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cycle_id: str, rows: Any) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(store, "save_fetch_log", boom)
    result = run(store, [FakeFetcher("searxng", article)])
    assert isinstance(result, NewsResult) and result.news_status == "ok" and result.facts
    assert persisted(store)[0]["news_status"] == "ok"
