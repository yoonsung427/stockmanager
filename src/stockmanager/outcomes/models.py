"""Outcome-scoring records and the hit-rate roll-up (v2 design D5, requirements O1-O3, rulings R8 and R12).

A score is derived from persisted execution facts alone: the fill (direction, shares, price, date) plus benchmark and
ticker closes from the price seam. It never reads LLM text: ``thesis`` is empty for sells and ``stance`` /
``thesis_status`` are not persisted domain fields (R8).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from stockmanager.domain.money import q4

#: Horizons scored, in calendar days after the fill.
HORIZONS: tuple[int, ...] = (30, 60, 90)
#: Only this horizon gates the hit-rate (R8); 30 and 60 are trend reporting.
GATE_HORIZON = 90
#: R12: below this many scored trades at the gate horizon the hit-rate is provisional and not an inference.
MIN_SCORED = 10
#: ``evaluations/`` file holding every score, one JSON object per line. ``.jsonl`` on purpose: the dashboard reads
#: ``evaluations/*.json`` as evaluation summaries and must not see this file.
SCORES_FILE = "outcomes-scores.jsonl"


class OutcomeScore(BaseModel):
    """One executed trade scored at one horizon against the benchmark.

    ``fraction`` is the share of the position a SELL closed (``None`` for a BUY); the return measured is that of the
    traded shares from the fill price, so a trim is scored on the trimmed fraction only (R8).
    """

    model_config = ConfigDict(extra="forbid")

    trade_id: str
    action: Literal["BUY", "SELL"]
    ticker: str
    fill_date: dt.date
    horizon_days: int
    horizon_date: dt.date
    fraction: Decimal | None
    traded_value: Decimal
    ticker_return_pct: Decimal
    benchmark: str
    benchmark_return_pct: Decimal
    excess_pct: Decimal
    win: bool
    price_source: Literal["yahoo", "finnhub"]

    @property
    def key(self) -> tuple[str, int]:
        """``(trade_id, horizon_days)``: a score exists at most once per key."""
        return (self.trade_id, self.horizon_days)


def is_win(action: Literal["BUY", "SELL"], excess_pct: Decimal) -> bool:
    """R8 relative-performance convention: a BUY wins when the ticker beat the benchmark, a SELL when it lagged it.

    A tie is not a win.
    """
    return excess_pct > 0 if action == "BUY" else excess_pct < 0


class HorizonTrend(BaseModel):
    """Trend line for one horizon: how many trades are scored and their mean excess return."""

    model_config = ConfigDict(extra="forbid")

    horizon_days: int
    scored: int
    mean_excess_pct: Decimal | None


class HitRate(BaseModel):
    """Hit-rate at the gate horizon, plus 30/60/90 trend lines.

    ``hit_rate_pct`` is ``None`` while ``provisional`` (fewer than ``MIN_SCORED`` scored trades): the counts are
    reported and no inference is drawn (R12). The flip is automatic when the count reaches ``MIN_SCORED``.
    """

    model_config = ConfigDict(extra="forbid")

    scored: int
    wins: int
    min_scored: int
    provisional: bool
    hit_rate_pct: Decimal | None
    trends: list[HorizonTrend]


def hit_rate(scores: Iterable[OutcomeScore], *, min_scored: int = MIN_SCORED) -> HitRate:
    """Roll ``scores`` up: the 90-day horizon gates the hit-rate, 30/60 are reported as trends only."""
    all_scores = list(scores)
    gate = [s for s in all_scores if s.horizon_days == GATE_HORIZON]
    wins = sum(1 for s in gate if s.win)
    provisional = len(gate) < min_scored
    trends = []
    for h in HORIZONS:
        at = [s.excess_pct for s in all_scores if s.horizon_days == h]
        mean = q4(sum(at, Decimal(0)) / len(at)) if at else None
        trends.append(HorizonTrend(horizon_days=h, scored=len(at), mean_excess_pct=mean))
    return HitRate(
        scored=len(gate),
        wins=wins,
        min_scored=min_scored,
        provisional=provisional,
        hit_rate_pct=None if provisional else q4(Decimal(wins) * 100 / len(gate)),
        trends=trends,
    )


def describe(rate: HitRate) -> str:
    """One plain line for the digest: counts when provisional, the percentage once the gate is reached."""
    if rate.provisional:
        return (
            f"Hit-rate (90d vs benchmark): provisional, {rate.wins} wins of {rate.scored} scored "
            f"(need {rate.min_scored})."
        )
    return f"Hit-rate (90d vs benchmark): {rate.hit_rate_pct}% ({rate.wins} of {rate.scored} scored trades)."
