"""Query plan (AS §7.2, news-sources §3.1). Query text comes from templates and tickers, never from news text."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from stockmanager.domain.docs import PortfolioDoc
from stockmanager.news.config import NewsConfig, QueryKind


@dataclass(frozen=True)
class NewsQuery:
    """One fetch request. ``ticker`` is the holding, ETF or benchmark; macro queries use the benchmark."""

    kind: QueryKind
    ticker: str
    window_from: date
    window_to: date
    text: str  # search string for keyword adapters


def build_plan(
    portfolio: PortfolioDoc, config: NewsConfig, today: date, last_macro_success: date | None = None
) -> list[NewsQuery]:
    """Plan in priority order: each holding (news, analyst, earnings), benchmark, sector ETFs, macro.

    Macro is planned only when it has never succeeded or the last success is ``macro_cadence_days`` old.
    """
    back = today - timedelta(days=config.lookback_days)
    ahead = today + timedelta(days=config.earnings_horizon_days)
    plan: list[NewsQuery] = []
    for ticker in dict.fromkeys(p.ticker for p in portfolio.positions):
        plan.append(NewsQuery("holding", ticker, back, today, f"{ticker} stock"))
        plan.append(NewsQuery("analyst", ticker, back, today, f"{ticker} analyst upgrade OR downgrade OR price target"))
        plan.append(NewsQuery("earnings", ticker, today, ahead, f"{ticker} earnings date"))
    benchmark = config.benchmark
    plan.append(NewsQuery("benchmark", benchmark, back, today, f"{config.benchmark_name or benchmark} market"))
    for etf in dict.fromkeys(config.sector_etfs):
        if etf != benchmark:
            plan.append(NewsQuery("sector", etf, back, today, f"{etf} sector stocks"))
    if last_macro_success is None or (today - last_macro_success).days >= config.macro_cadence_days:
        macro_from = today - timedelta(days=config.macro_cadence_days)
        plan.append(NewsQuery("macro", benchmark, macro_from, today, "stock market economy Federal Reserve"))
    return plan
