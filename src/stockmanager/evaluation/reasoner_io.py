"""Reasoner input and output contracts (AS §7.3, §8.1, LP §4.4-4.5).

Every decimal is a string (AS §3.0): the models hold ``str`` and a validator checks it parses as a finite ``Decimal``,
so nothing ever passes through ``float``. The output is *intent* only; it has no field for prices, share counts, IDs,
deadlines or settings (TS INJ-08).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from stockmanager.domain.docs import NewsEvent


def _decimal_string(value: str) -> str:
    try:
        parsed = Decimal(value)
    except InvalidOperation as e:
        raise ValueError(f"not a decimal string: {value!r}") from e
    if not parsed.is_finite():
        raise ValueError(f"not a finite decimal: {value!r}")
    return value


DecStr = Annotated[str, AfterValidator(_decimal_string)]
"""A decimal carried as a string, for example ``"0.2500"``."""


class _IO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# -- input -------------------------------------------------------------------------------------------------------


class ReasonerConfig(_IO):
    """The part of config the model may see. Limits are rendered once, into the system prompt (LP §4.4)."""

    benchmark: str
    approval_mode: Literal["auto", "explicit"]
    veto_window_hours: int = Field(ge=1)
    evaluation_frequency: Literal["daily", "weekly", "biweekly"]


class PositionSnapshot(_IO):
    """One holding. Momentum fields are ``None`` when data-poor (AS §1.2), never ``0``."""

    ticker: str
    sector: str
    shares: DecStr
    avg_cost: DecStr
    price: DecStr
    value: DecStr
    weight_pct: DecStr
    return_since_fill_pct: DecStr | None
    return_1w_pct: DecStr | None
    return_2w_pct: DecStr | None
    rel_strength_vs_benchmark_pct: DecStr | None
    held_since: dt.date
    thesis: str


class PortfolioSnapshot(_IO):
    """Valuation of the books at the valuation date's closes."""

    nav: DecStr
    cash: DecStr
    cash_pct: DecStr
    positions: list[PositionSnapshot]


class BenchmarkSnapshot(_IO):
    """Portfolio versus benchmark since go-live."""

    ticker: str
    roi_pct: DecStr | None
    portfolio_roi_pct: DecStr | None
    relative_pct: DecStr | None


class FlaggedCorrelation(_IO):
    """A pair of holdings whose correlation exceeded the alert threshold."""

    a: str
    b: str
    corr: DecStr


class RiskMetrics(_IO):
    """Risk figures computed by the system."""

    max_drawdown_pct: DecStr | None
    volatility_weekly_pct: DecStr | None
    flagged_correlations: list[FlaggedCorrelation]
    alerts_active: list[str]


class SectorWeight(_IO):
    """Share of NAV held in one sector; cash excluded."""

    sector: str
    weight_pct: DecStr
    tickers: list[str]


class SectorETF(_IO):
    """Sector-ETF returns, for rotation (SS §8.1)."""

    etf: str
    sector: str
    return_1w_pct: DecStr | None
    return_2w_pct: DecStr | None


class FactProjection(_IO):
    """``Fact.for_reasoner()``: no ``display_title``, no ``source_url`` (AS §7.3)."""

    fact_id: str
    ticker: str
    event: NewsEvent
    description: str
    date: dt.date
    source_domain: str


class VetoLeg(_IO):
    """One leg of a vetoed proposal, stripped to what the model may see."""

    action: Literal["BUY", "SELL"]
    ticker: str


class VetoRecord(_IO):
    """A past veto. The proposal ID is replaced by an ordinal so the model has no ID to echo (LP §4.4)."""

    proposal_id_redacted: str
    vetoed_on: dt.date
    legs: list[VetoLeg]
    reason: str | None = Field(max_length=500)


MAX_VETOES = 10
#: Cap on raw outcome records rendered to the model (O3); the stats block is a fixed handful of fields.
MAX_OUTCOME_RECORDS = 10


class OutcomeRecord(_IO):
    """One scored trade, stripped to what the model may see: an ordinal, never the trade or proposal ID (LP §4.4).

    ``win`` is ``None`` for 30/60-day trend rows and for every row while the section is provisional (R12).
    """

    ordinal: str
    action: Literal["BUY", "SELL"]
    ticker: str
    fill_date: dt.date
    horizon_days: int
    fraction: DecStr | None
    excess_pct: DecStr
    win: bool | None


class OutcomeStats(_IO):
    """90-day hit-rate. Present only once ``OutcomesSection.min_scored`` trades are scored (R12)."""

    scored: int
    wins: int
    hit_rate_pct: DecStr


class OutcomesSection(_IO):
    """Outcomes/stats block (O3). System-computed from fills and prices; carries no free text from any source.

    ``provisional`` is true below ``min_scored`` scored 90-day trades: only raw records are rendered, ``stats`` is
    ``None``. At or above it ``provisional`` flips to false and ``stats`` appears, with no human gate (R12).
    """

    provisional: bool = True
    min_scored: int = 10
    scored_90d: int = 0
    records: list[OutcomeRecord] = Field(default_factory=list, max_length=MAX_OUTCOME_RECORDS)
    stats: OutcomeStats | None = None


class ReasonerInput(_IO):
    """Everything the Reasoner sees, built from validated, persisted data. There is no raw-news field."""

    cycle_id: str
    valuation_date: str
    price_source: Literal["yahoo", "finnhub"]
    config: ReasonerConfig
    portfolio: PortfolioSnapshot
    benchmark: BenchmarkSnapshot
    metrics: RiskMetrics
    sector_weights: list[SectorWeight]
    sector_etfs: list[SectorETF]
    facts: list[FactProjection]
    veto_history: list[VetoRecord] = Field(max_length=MAX_VETOES)
    outcomes: OutcomesSection


# -- output ------------------------------------------------------------------------------------------------------


class FiveDims(_IO):
    """The five assessment dimensions (SS §8.1)."""

    thesis: str
    momentum: str
    catalysts: str
    risk: str
    opportunity_cost: str


class HoldingAssessment(_IO):
    """Stance and thesis status for one current holding."""

    ticker: str
    stance: Literal["hold", "add", "trim", "exit"]
    thesis_status: Literal["intact", "strengthened", "weakened", "broken", "unclear"]
    dims: FiveDims
    evidence_fact_ids: list[str]


class SizeIntent(_IO):
    """Sizing intent. The combination of fields must match ``kind``; anything else is malformed."""

    kind: Literal["fraction_of_position", "proceeds_of", "target_value"]
    fraction: DecStr | None
    leg: str | None
    value: DecStr | None

    @model_validator(mode="after")
    def _combination(self) -> SizeIntent:
        have = {k for k in ("fraction", "leg", "value") if getattr(self, k) is not None}
        want = {
            "fraction_of_position": {"fraction"},
            "proceeds_of": {"fraction", "leg"},
            "target_value": {"value"},
        }[self.kind]
        if have != want:
            raise ValueError(f"{self.kind} needs exactly {sorted(want)}, got {sorted(have)}")
        return self


class TradeLeg(_IO):
    """One intended trade: an action, a ticker and a sizing intent."""

    leg: str
    action: Literal["BUY", "SELL"]
    ticker: str
    thesis: str | None
    size_intent: SizeIntent


class LimitsCheck(_IO):
    """The model's self-attestation per limit; audit-only (LP §4.6)."""

    position_cap: bool
    new_position_cap: bool
    sector_cap: bool
    trade_cap: bool
    long_only: bool
    cash: bool


class RiskAssessment(_IO):
    """The model's own estimates for audit; never used for sizing or enforcement."""

    sector: str
    largest_position_after_pct: DecStr
    sector_weight_after_pct: DecStr
    main_risks: list[str]
    invalidation: str
    confidence: Literal["low", "medium", "high"]
    limits_check: LimitsCheck


class TradeIdea(_IO):
    """A set of legs that stand or fall together."""

    priority: int
    rationale: str
    why_beats_hold: str
    legs: list[TradeLeg]
    risk_assessment: RiskAssessment
    evidence_fact_ids: list[str]


class ReasonerOutput(_IO):
    """The Reasoner's whole answer: assessments, ordered ideas, and the hold alternative."""

    summary: str
    hold_alternative: str
    holdings: list[HoldingAssessment]
    ideas: list[TradeIdea]
