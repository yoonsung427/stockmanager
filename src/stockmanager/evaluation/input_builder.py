"""Builds ``ReasonerInput`` from persisted, typed data (AS §7.3): the ``ReasonerInputBuilder`` the engine calls.

Deliberately conservative: several momentum/risk fields that would need a dedicated historical price series
(``return_1w_pct``, ``rel_strength_vs_benchmark_pct``, sector-ETF returns, correlations) are left ``None`` rather
than approximated, matching AS §1.2's "data-poor returns ``None``, never ``0``" rule. ``nav_history.jsonl`` --
appended by the execution engine at inception and at close -- is the only return series available to this build,
so drawdown/volatility are derived from it when present.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal

from stockmanager.config.model import Config
from stockmanager.domain import metrics
from stockmanager.domain.docs import PortfolioDoc, PriceTable
from stockmanager.evaluation.reasoner_io import (
    BenchmarkSnapshot,
    FactProjection,
    PortfolioSnapshot,
    PositionSnapshot,
    ReasonerConfig,
    ReasonerInput,
    RiskMetrics,
    SectorWeight,
    VetoLeg,
    VetoRecord,
)
from stockmanager.news import NewsResult
from stockmanager.store import FileStore

MAX_VETOES = 10


def _nav_series(store: FileStore) -> list[Decimal]:
    """NAV observations from ``nav_history.jsonl`` in file order, oldest first; ``[]`` if the file is absent."""
    path = store.nav_history_path
    if not path.exists():
        return []
    out: list[Decimal] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(Decimal(json.loads(line)["nav"]))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def _veto_history(store: FileStore) -> list[VetoRecord]:
    """Up to ``MAX_VETOES`` most recent vetoes, newest first, proposal IDs replaced by an ordinal (LP §4.4)."""
    from stockmanager.domain.docs import ProposalDoc

    directory = store.root / "proposals"
    if not directory.exists():
        return []
    vetoed = []
    for path in sorted(directory.glob("*.json")):
        try:
            doc = ProposalDoc.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if doc.state == "VETOED":
            vetoed.append(doc)
    vetoed.sort(key=lambda d: d.created_at, reverse=True)
    records: list[VetoRecord] = []
    for i, doc in enumerate(vetoed[:MAX_VETOES], start=1):
        last = doc.transitions[-1]
        reason = last.cause if last.cause and last.cause != "vetoed" else None
        records.append(
            VetoRecord(
                proposal_id_redacted=f"V{i}",
                vetoed_on=last.at.date(),
                legs=[VetoLeg(action=leg.action, ticker=leg.ticker) for leg in doc.legs],
                reason=reason[:500] if reason else None,
            )
        )
    return records


ReasonerInputBuilder = Callable[[str, PortfolioDoc, PriceTable, NewsResult, Config], ReasonerInput]


def make_reasoner_input_builder(store: FileStore) -> ReasonerInputBuilder:
    """Return a ``ReasonerInputBuilder`` closing over ``store`` for ``nav_history.jsonl`` and veto history."""

    def build(cycle_id: str, snapshot: PortfolioDoc, valuation: PriceTable, news: NewsResult, cfg: Config) -> ReasonerInput:
        prices = {t: q.price for t, q in valuation.quotes.items()}
        held = {p.ticker: p.shares for p in snapshot.positions}
        position_values = {t: held[t] * prices[t] for t in held}
        nav = snapshot.cash + sum(position_values.values(), Decimal(0))
        cash_pct = (snapshot.cash / nav * 100) if nav > 0 else Decimal(0)
        weights = metrics.position_weights(position_values, snapshot.cash)

        positions = [
            PositionSnapshot(
                ticker=p.ticker,
                sector="Unclassified",
                shares=str(p.shares),
                avg_cost=str(p.fill_price),
                price=str(prices[p.ticker]),
                value=str(position_values[p.ticker]),
                weight_pct=str(weights.get(p.ticker, Decimal(0))),
                return_since_fill_pct=(
                    str((prices[p.ticker] - p.fill_price) / p.fill_price * 100) if p.fill_price > 0 else None
                ),
                return_1w_pct=None,
                return_2w_pct=None,
                rel_strength_vs_benchmark_pct=None,
                held_since=p.fill_time.date(),
                thesis=p.thesis,
            )
            for p in snapshot.positions
        ]

        nav_series = _nav_series(store)
        drawdown = metrics.max_drawdown(nav_series) if nav_series else None
        weekly = metrics._resample_weekly(nav_series) if nav_series else []
        volatility = metrics.volatility(weekly) if weekly else None
        portfolio_roi = metrics.roi(snapshot.start_cash, nav)
        sector_w = metrics.sector_weights(position_values, {}, snapshot.cash)

        return ReasonerInput(
            cycle_id=cycle_id,
            valuation_date=valuation.session_date.isoformat(),
            price_source=valuation.source,
            config=ReasonerConfig(
                benchmark=cfg.benchmark,
                approval_mode="auto" if cfg.auto_approve else "explicit",
                veto_window_hours=cfg.veto_window_hours,
                evaluation_frequency=cfg.evaluation_frequency,
            ),
            portfolio=PortfolioSnapshot(nav=str(nav), cash=str(snapshot.cash), cash_pct=str(cash_pct), positions=positions),
            benchmark=BenchmarkSnapshot(
                ticker=cfg.benchmark,
                roi_pct=None,
                portfolio_roi_pct=str(portfolio_roi) if portfolio_roi is not None else None,
                relative_pct=None,
            ),
            metrics=RiskMetrics(
                max_drawdown_pct=str(drawdown) if drawdown is not None else None,
                volatility_weekly_pct=str(volatility) if volatility is not None else None,
                flagged_correlations=[],
                alerts_active=[],
            ),
            sector_weights=[
                SectorWeight(sector=sector, weight_pct=str(w), tickers=sorted(position_values))
                for sector, w in sector_w.items()
            ],
            sector_etfs=[],
            facts=[FactProjection.model_validate(f.for_reasoner()) for f in news.facts],
            veto_history=_veto_history(store),
        )

    return build
