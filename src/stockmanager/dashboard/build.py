"""Pure function: state files -> ``DashboardModel`` (AS §9.3).

``build_dashboard`` only reads; it never locks, writes or calls the network. It is safe to call on demand
(``stockmanager dashboard build``) and as the last step of a committed ``Transaction``.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from stockmanager.dashboard.model import (
    DashboardModel,
    EarningEvent,
    EvaluationSummary,
    NAVPoint,
    NewsFact,
    OutcomesSummary,
    PendingProposal,
    PositionRow,
    RiskSummary,
    SectorWeight,
    Trade,
)
from stockmanager.domain.docs import PortfolioDoc, ProposalDoc
from stockmanager.domain.money import q4
from stockmanager.outcomes.models import hit_rate
from stockmanager.outcomes.scoring import load_scores
from stockmanager.store import FileStore
from stockmanager.store.lock import Clock

HUNDRED = Decimal(100)
EARNINGS_WINDOW_DAYS = 30
RECENT_TRADES_LIMIT = 10
NEWS_PER_TICKER_LIMIT = 10
NONTERMINAL_PROPOSAL_STATES = {"PROPOSED", "VETO_WINDOW", "APPROVED"}


def build_dashboard(store: FileStore, clock: Clock) -> DashboardModel:
    """Read ``store``'s state files and compute the ``DashboardModel`` as of ``clock()``.

    Pure: no writes, no locks, no network calls. All figures come from already-persisted, validated files.
    """
    now = clock()
    today = now.date()
    portfolio = store.load_portfolio()
    nav_rows = _load_nav_history(store.nav_history_path)
    facts = _load_facts(store.root / "extracted-facts")
    proposals = _load_proposals(store.root / "proposals")
    eval_rows = _load_evaluations(store.root / "evaluations")

    prices = _latest_prices(nav_rows)
    sector_map = _sector_map(portfolio.config)
    benchmark_ticker = str(portfolio.config.get("benchmark", "SPY"))

    position_rows, positions_value = _build_positions(portfolio, prices)
    nav = q4(positions_value + portfolio.cash)
    cash_pct = q4(portfolio.cash / nav * HUNDRED) if nav > 0 else Decimal(0)
    for i, row in enumerate(position_rows):
        if row.value is not None and nav > 0:
            position_rows[i] = _with_weight(row, q4(row.value / nav * HUNDRED))

    nav_history, benchmark_history = _nav_series(nav_rows, nav, today)
    nav_pct_change = _roi(nav_history, portfolio.start_cash)
    benchmark_roi_pct = _roi_from_rebased(benchmark_history)
    relative_pct = q4(nav_pct_change - benchmark_roi_pct) if nav_pct_change is not None and benchmark_roi_pct is not None else None

    raw_nav_values = [portfolio.start_cash] + [r.value for r in nav_history]
    max_drawdown_pct = _max_drawdown_pct(raw_nav_values)
    days_active = (today - nav_rows[0]["date"]).days if nav_rows else None

    sector_weights = _sector_weights(position_rows, sector_map, portfolio.cash, nav)
    risk = RiskSummary(
        max_drawdown_pct=max_drawdown_pct,
        volatility_weekly_pct=_weekly_volatility(raw_nav_values),
        sharpe=_sharpe(raw_nav_values, Decimal(str(portfolio.config.get("risk_free_rate_annual", 0)))),
        largest_position_pct=max((r.weight_pct for r in position_rows if r.weight_pct is not None), default=None),
        sector_concentration=max(
            (s.weight_pct for s in sector_weights if s.sector != "Cash"), default=None
        ),
        correlation_flags=None,
    )

    recent_trades = [_to_trade_row(t) for t in sorted(portfolio.trades, key=lambda t: t.timestamp, reverse=True)][
        :RECENT_TRADES_LIMIT
    ]

    held_tickers = {p.ticker for p in portfolio.positions}
    upcoming_earnings = _upcoming_earnings(facts, held_tickers, today)
    news_by_ticker = _news_by_ticker(facts, held_tickers)

    evaluations = sorted(eval_rows, key=lambda e: e.cycle_date, reverse=True)
    pending_proposals = [
        _to_pending(p) for p in proposals if p.state in NONTERMINAL_PROPOSAL_STATES
    ]
    pending_proposals.sort(key=lambda p: p.created_at, reverse=True)

    return DashboardModel(
        nav=nav,
        nav_pct_change=nav_pct_change,
        benchmark_ticker=benchmark_ticker,
        benchmark_roi_pct=benchmark_roi_pct,
        relative_pct=relative_pct,
        max_drawdown_pct=max_drawdown_pct,
        days_active=days_active,
        last_updated=now,
        positions=position_rows,
        cash=portfolio.cash,
        cash_pct=cash_pct,
        nav_history=nav_history,
        benchmark_history=benchmark_history,
        sector_weights=sector_weights,
        risk=risk,
        recent_trades=recent_trades,
        upcoming_earnings=upcoming_earnings,
        news_by_ticker=news_by_ticker,
        evaluations=evaluations,
        status=portfolio.status,
        auto_approve=bool(portfolio.config.get("auto_approve", True)),
        pending_proposals=pending_proposals,
        outcomes=_outcomes_summary(store),
    )


def _outcomes_summary(store: FileStore) -> OutcomesSummary:
    rate = hit_rate(load_scores(store.root))
    return OutcomesSummary(
        scored=rate.scored,
        wins=rate.wins,
        min_scored=rate.min_scored,
        provisional=rate.provisional,
        hit_rate_pct=rate.hit_rate_pct,
        trends=[(t.horizon_days, t.scored, t.mean_excess_pct) for t in rate.trends],
    )


# -- loading raw files -----------------------------------------------------------------------------------------


def _load_nav_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        row["date"] = date.fromisoformat(row["date"])
        rows.append(row)
    rows.sort(key=lambda r: r["date"])
    return rows


def _load_facts(directory: Path) -> list[dict[str, Any]]:
    """Facts from every ``extracted-facts/<cycle>.json`` file, de-duplicated by ``fact_id``."""
    if not directory.exists():
        return []
    by_id: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for fact in doc.get("facts", []):
            by_id[fact["fact_id"]] = fact
    return list(by_id.values())


def _load_proposals(directory: Path) -> list[ProposalDoc]:
    if not directory.exists():
        return []
    out: list[ProposalDoc] = []
    for path in sorted(directory.glob("*.json")):
        try:
            out.append(ProposalDoc.model_validate_json(path.read_text(encoding="utf-8")))
        except ValueError:
            continue  # quarantined: unparsable proposal files are excluded from every job (AS §10.1)
    return out


def _load_evaluations(directory: Path) -> list[EvaluationSummary]:
    if not directory.exists():
        return []
    out: list[EvaluationSummary] = []
    for path in sorted(directory.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        out.append(_evaluation_summary(path.stem, doc))
    return out


def _evaluation_summary(stem: str, doc: dict[str, Any]) -> EvaluationSummary:
    eval_id = str(doc.get("eval_id", stem))
    cycle_date_raw = doc.get("cycle_date") or doc.get("cycle_id") or stem.removeprefix("eval-")
    cycle_date = date.fromisoformat(str(cycle_date_raw))
    summary = str(doc.get("summary", ""))
    ideas = doc.get("ideas", [])
    proposals = doc.get("proposals", [])
    return EvaluationSummary(
        eval_id=eval_id,
        cycle_date=cycle_date,
        summary=summary,
        ideas_count=int(doc.get("ideas_count", len(ideas))),
        proposals_count=int(doc.get("proposals_count", len(proposals))),
    )


# -- positions, prices, sectors ---------------------------------------------------------------------------------


def _latest_prices(nav_rows: list[dict[str, Any]]) -> dict[str, Decimal]:
    if not nav_rows:
        return {}
    prices = nav_rows[-1].get("prices", {})
    return {ticker: Decimal(str(price)) for ticker, price in prices.items()}


def _sector_map(config: dict[str, Any]) -> dict[str, str]:
    raw = config.get("sectors", {})
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _build_positions(portfolio: PortfolioDoc, prices: dict[str, Decimal]) -> tuple[list[PositionRow], Decimal]:
    rows: list[PositionRow] = []
    total = Decimal(0)
    for p in portfolio.positions:
        current_price = prices.get(p.ticker, p.fill_price)  # fallback to cost basis (TS MET-11)
        value = q4(p.shares * current_price)
        total += value
        return_pct = q4((current_price - p.fill_price) / p.fill_price * HUNDRED) if p.fill_price > 0 else None
        rows.append(
            PositionRow(
                ticker=p.ticker,
                thesis=p.thesis,
                shares=p.shares,
                avg_cost=p.fill_price,
                current_price=current_price,
                value=value,
                return_pct=return_pct,
                weight_pct=None,  # filled once NAV is known
            )
        )
    return rows, total


def _with_weight(row: PositionRow, weight_pct: Decimal) -> PositionRow:
    return PositionRow(
        ticker=row.ticker,
        thesis=row.thesis,
        shares=row.shares,
        avg_cost=row.avg_cost,
        current_price=row.current_price,
        value=row.value,
        return_pct=row.return_pct,
        weight_pct=weight_pct,
    )


def _sector_weights(
    rows: list[PositionRow], sector_map: dict[str, str], cash: Decimal, nav: Decimal
) -> list[SectorWeight]:
    by_sector: dict[str, list[str]] = {}
    value_by_sector: dict[str, Decimal] = {}
    for row in rows:
        sector = sector_map.get(row.ticker, "Unclassified")
        by_sector.setdefault(sector, []).append(row.ticker)
        value_by_sector[sector] = value_by_sector.get(sector, Decimal(0)) + (row.value or Decimal(0))
    weights = [
        SectorWeight(
            sector=sector,
            weight_pct=q4(value_by_sector[sector] / nav * HUNDRED) if nav > 0 else Decimal(0),
            tickers=sorted(tickers),
        )
        for sector, tickers in by_sector.items()
    ]
    weights.sort(key=lambda s: s.weight_pct, reverse=True)
    if nav > 0:
        weights.append(SectorWeight(sector="Cash", weight_pct=q4(cash / nav * HUNDRED), tickers=[]))
    return weights


# -- NAV series, ROI, drawdown, volatility, Sharpe ---------------------------------------------------------------


def _nav_series(
    nav_rows: list[dict[str, Any]], current_nav: Decimal, today: date
) -> tuple[list[NAVPoint], list[NAVPoint]]:
    nav_history = [NAVPoint(date=r["date"], value=Decimal(str(r["nav"]))) for r in nav_rows]
    if not nav_history or nav_history[-1].date != today:
        nav_history.append(NAVPoint(date=today, value=current_nav))

    benchmark_closes = [(r["date"], Decimal(str(r["benchmark_close"]))) for r in nav_rows if "benchmark_close" in r]
    if not benchmark_closes:
        return nav_history, []
    base = benchmark_closes[0][1]
    benchmark_history = [NAVPoint(date=d, value=q4(close / base * HUNDRED)) for d, close in benchmark_closes]
    return nav_history, benchmark_history


def _roi(nav_history: list[NAVPoint], start_cash: Decimal) -> Decimal | None:
    if not nav_history or start_cash <= 0:
        return None
    return q4((nav_history[-1].value - start_cash) / start_cash * HUNDRED)


def _roi_from_rebased(benchmark_history: list[NAVPoint]) -> Decimal | None:
    if len(benchmark_history) < 2:
        return None
    return q4(benchmark_history[-1].value - HUNDRED)


def _max_drawdown_pct(values: list[Decimal]) -> Decimal | None:
    """Peak-to-trough decline since inception, a non-negative percentage (TS D3)."""
    if not values:
        return None
    peak = values[0]
    worst = Decimal(0)
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return q4(worst * HUNDRED)


def _weekly_returns(values: list[Decimal]) -> list[float]:
    if len(values) < 2:
        return []
    return [float((b - a) / a) for a, b in pairwise(values) if a > 0]


def _weekly_volatility(values: list[Decimal]) -> Decimal | None:
    """Sample stdev (n-1) of weekly returns; ``None`` below 3 NAV points (TS D8)."""
    returns = _weekly_returns(values)
    if len(returns) < 2:
        return None
    return q4(Decimal(str(statistics.stdev(returns) * 100)))


def _sharpe(values: list[Decimal], risk_free_rate_annual: Decimal) -> Decimal | None:
    """Mean(weekly return - rf/52) / stdev * sqrt(52); ``None`` below 3 returns or zero stdev (TS D2, D8)."""
    returns = _weekly_returns(values)
    if len(returns) < 3:
        return None
    stdev = statistics.stdev(returns)
    if stdev == 0:
        return None
    rf_weekly = float(risk_free_rate_annual) / 52
    mean_excess = statistics.mean(r - rf_weekly for r in returns)
    return q4(Decimal(str(mean_excess / stdev * (52**0.5))))


# -- trades, earnings, news, proposals ----------------------------------------------------------------------------


def _to_trade_row(t: Any) -> Trade:
    return Trade(
        trade_id=t.trade_id,
        timestamp=t.timestamp,
        action=t.action,
        ticker=t.ticker,
        shares=t.shares,
        fill_price=t.fill_price,
        value=q4(t.shares * t.fill_price),
    )


def _upcoming_earnings(facts: Iterable[dict[str, Any]], held: set[str], today: date) -> list[EarningEvent]:
    out: list[EarningEvent] = []
    for f in facts:
        if f.get("event") != "earnings" or f.get("ticker") not in held:
            continue
        event_date = date.fromisoformat(f["date"])
        if today <= event_date <= today + timedelta(days=EARNINGS_WINDOW_DAYS):
            out.append(EarningEvent(ticker=f["ticker"], date=event_date, event=f["event"]))
    out.sort(key=lambda e: e.date)
    return out


def _news_by_ticker(facts: Iterable[dict[str, Any]], held: set[str]) -> dict[str, list[NewsFact]]:
    grouped: dict[str, list[NewsFact]] = {}
    for f in facts:
        ticker = f.get("ticker")
        if ticker not in held:
            continue
        grouped.setdefault(ticker, []).append(
            NewsFact(
                fact_id=f["fact_id"],
                ticker=ticker,
                event=f["event"],
                description=f["description"],
                date=date.fromisoformat(f["date"]),
                source_url=f.get("source_url", ""),
                source_domain=f.get("source_domain", ""),
                display_title=f.get("display_title", ""),
            )
        )
    for ticker, items in grouped.items():
        items.sort(key=lambda n: n.date, reverse=True)
        grouped[ticker] = items[:NEWS_PER_TICKER_LIMIT]
    return grouped


def _legs_summary(proposal: ProposalDoc) -> str:
    parts = [f"{leg.action} {leg.ticker}" for leg in proposal.legs]
    sells = [p for p, leg in zip(parts, proposal.legs) if leg.action == "SELL"]
    buys = [p for p, leg in zip(parts, proposal.legs) if leg.action == "BUY"]
    if sells and buys:
        return f"{' + '.join(sells)} \u2192 {' + '.join(buys)}"
    return " + ".join(parts)


def _to_pending(proposal: ProposalDoc) -> PendingProposal:
    return PendingProposal(
        proposal_id=proposal.proposal_id,
        created_at=proposal.created_at,
        state=proposal.state,
        legs_summary=_legs_summary(proposal),
        veto_deadline=proposal.veto_deadline,
        expires_at=proposal.expires_at,
        command_literal=f"VETO {proposal.proposal_id}",
    )
