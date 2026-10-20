"""``DashboardModel``: the pure, JSON-serializable data model behind the dashboard (AS §9.3).

Every field is derived from state files; nothing here does I/O. Data-poor metrics are ``None`` and the templates
render them as "n/a" (TS MET-11, DASH-09). All money and share figures are ``Decimal``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class PositionRow:
    """One holding as rendered in the ``holdings`` table."""

    ticker: str
    thesis: str
    shares: Decimal
    avg_cost: Decimal
    current_price: Decimal | None
    value: Decimal | None
    return_pct: Decimal | None
    weight_pct: Decimal | None


@dataclass(frozen=True)
class NAVPoint:
    """One point of a NAV (or rebased benchmark) time series."""

    date: dt.date
    value: Decimal


@dataclass(frozen=True)
class SectorWeight:
    """Share of NAV held in one sector (cash is a separate pseudo-sector, never "Unclassified")."""

    sector: str
    weight_pct: Decimal
    tickers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RiskSummary:
    """Risk figures for the ``risk`` section; any field may be ``None`` when data-poor."""

    max_drawdown_pct: Decimal | None
    volatility_weekly_pct: Decimal | None
    sharpe: Decimal | None
    largest_position_pct: Decimal | None
    sector_concentration: Decimal | None
    correlation_flags: list[str] | None = None


@dataclass(frozen=True)
class EarningEvent:
    """A holding's upcoming earnings-adjacent date (from facts, within 30 days)."""

    ticker: str
    date: dt.date
    event: str


@dataclass(frozen=True)
class NewsFact:
    """A news fact as rendered in the ``news`` section (display-only fields included)."""

    fact_id: str
    ticker: str
    event: str
    description: str
    date: dt.date
    source_url: str
    source_domain: str
    display_title: str


@dataclass(frozen=True)
class EvaluationSummary:
    """One line per evaluation cycle that produced proposals or assessments."""

    eval_id: str
    cycle_date: dt.date
    summary: str
    ideas_count: int
    proposals_count: int


@dataclass(frozen=True)
class PendingProposal:
    """A proposal still awaiting its veto window or execution, for the status strip."""

    proposal_id: str
    created_at: dt.datetime
    state: str
    legs_summary: str
    veto_deadline: dt.datetime | None
    expires_at: dt.datetime
    command_literal: str


@dataclass(frozen=True)
class Trade:
    """One fill, as rendered in the ``activity`` section."""

    trade_id: str
    timestamp: dt.datetime
    action: str
    ticker: str
    shares: Decimal
    fill_price: Decimal
    value: Decimal


@dataclass(frozen=True)
class DashboardModel:
    """Everything the dashboard renders, in one serializable snapshot (AS §9.3)."""

    # Summary
    nav: Decimal
    nav_pct_change: Decimal | None
    benchmark_ticker: str
    benchmark_roi_pct: Decimal | None
    relative_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    days_active: int | None
    last_updated: dt.datetime

    # Holdings
    positions: list[PositionRow]
    cash: Decimal
    cash_pct: Decimal

    # Performance chart
    nav_history: list[NAVPoint]
    benchmark_history: list[NAVPoint]

    # Sectors
    sector_weights: list[SectorWeight]

    # Risk
    risk: RiskSummary

    # Activity
    recent_trades: list[Trade]

    # Earnings
    upcoming_earnings: list[EarningEvent]

    # News
    news_by_ticker: dict[str, list[NewsFact]]

    # Evaluations
    evaluations: list[EvaluationSummary]

    # Status
    status: str
    auto_approve: bool
    pending_proposals: list[PendingProposal]
