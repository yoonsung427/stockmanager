"""Query plan: holdings x 3 kinds + benchmark + sectors + macro (news-sources §3.1)."""

from datetime import date, timedelta

from stockmanager.news.config import NewsConfig
from stockmanager.news.plan import build_plan
from tests.support.news import TODAY, portfolio

CFG = NewsConfig(sector_etfs=["XLK", "XLF"])


def test_plan_shape_and_order() -> None:
    plan = build_plan(portfolio("AAPL", "MSFT"), CFG, TODAY)
    assert [(q.kind, q.ticker) for q in plan] == [
        ("holding", "AAPL"), ("analyst", "AAPL"), ("earnings", "AAPL"),
        ("holding", "MSFT"), ("analyst", "MSFT"), ("earnings", "MSFT"),
        ("benchmark", "SPY"), ("sector", "XLK"), ("sector", "XLF"), ("macro", "SPY"),
    ]  # fmt: skip


def test_windows() -> None:
    by_kind = {q.kind: q for q in build_plan(portfolio("AAPL"), CFG, TODAY)}
    assert (by_kind["holding"].window_from, by_kind["holding"].window_to) == (TODAY - timedelta(days=7), TODAY)
    assert (by_kind["earnings"].window_from, by_kind["earnings"].window_to) == (TODAY, TODAY + timedelta(days=30))
    assert by_kind["macro"].window_from == TODAY - timedelta(days=7)


def test_query_text_comes_from_templates() -> None:
    texts = {q.kind: q.text for q in build_plan(portfolio("AAPL"), CFG, TODAY)}
    assert texts["holding"] == "AAPL stock"
    assert texts["analyst"] == "AAPL analyst upgrade OR downgrade OR price target"
    assert texts["earnings"] == "AAPL earnings date"
    assert texts["macro"] == "stock market economy Federal Reserve"


def test_macro_follows_cadence() -> None:
    recent = build_plan(portfolio("AAPL"), CFG, TODAY, last_macro_success=TODAY - timedelta(days=3))
    due = build_plan(portfolio("AAPL"), CFG, TODAY, last_macro_success=TODAY - timedelta(days=7))
    assert "macro" not in {q.kind for q in recent}
    assert "macro" in {q.kind for q in due}


def test_no_holdings_still_covers_benchmark_sectors_macro() -> None:
    assert [q.kind for q in build_plan(portfolio(), CFG, TODAY)] == ["benchmark", "sector", "sector", "macro"]


def test_benchmark_is_not_repeated_as_a_sector() -> None:
    cfg = NewsConfig(benchmark="XLK", sector_etfs=["XLK", "XLF"])
    plan = build_plan(portfolio(), cfg, date(2026, 10, 19))
    assert [(q.kind, q.ticker) for q in plan if q.kind in ("benchmark", "sector")] == [
        ("benchmark", "XLK"),
        ("sector", "XLF"),
    ]
