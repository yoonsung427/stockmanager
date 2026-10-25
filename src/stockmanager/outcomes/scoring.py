"""Scores executed fills at +30/+60/+90 days against the benchmark (v2 design D5, O1, R8).

Everything here is computable from ``portfolio.json`` fills and closes from the price seam. Vetoed proposals never
produce a ``Trade`` and so are excluded by construction (Q5). A score is written once per ``(trade, horizon)`` when
the horizon session has completed; a price failure leaves that pair unscored and the next evaluation retries it, so no
executed trade stays unscored while the portfolio is live.

Return basis: the traded shares' return runs from the fill price to the horizon session's close; the benchmark's runs
from its close on the fill session to the horizon close. The ticker and benchmark horizon closes come from one source.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from stockmanager.domain.docs import PriceTable, Trade
from stockmanager.domain.money import q4
from stockmanager.domain.ports import PriceProvider, SessionCalendar
from stockmanager.domain.sessions import is_session, last_completed_session
from stockmanager.domain.timeutil import PT
from stockmanager.outcomes.models import HORIZONS, SCORES_FILE, OutcomeScore, is_win
from stockmanager.pricing.errors import PriceError, ValuationUnavailable
from stockmanager.pricing.service import PriceService

log = logging.getLogger(__name__)

_LOOKBACK_DAYS = 10
_HUNDRED = Decimal(100)


def scores_path(root: Path) -> Path:
    """``<workspace>/evaluations/outcomes-scores.jsonl``."""
    return root / "evaluations" / SCORES_FILE


def load_scores(root: Path) -> list[OutcomeScore]:
    """Every parseable score on disk, oldest fill first; ``[]`` when the file is absent. Bad lines are skipped."""
    path = scores_path(root)
    if not path.exists():
        return []
    out: list[OutcomeScore] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(OutcomeScore.model_validate_json(line))
        except ValueError:
            log.warning("outcomes: skipping unparsable score line")
    return sorted(out, key=_order)


def dump_scores(scores: Sequence[OutcomeScore]) -> str:
    """The text of ``outcomes-scores.jsonl``: one compact JSON object per line in stable order, trailing newline."""
    return "".join(s.model_dump_json() + "\n" for s in sorted(scores, key=_order))


def _order(s: OutcomeScore) -> tuple[date, str, int]:
    return (s.fill_date, s.trade_id, s.horizon_days)


def traded_fractions(trades: Sequence[Trade]) -> dict[str, Decimal | None]:
    """``trade_id -> fraction of the position sold`` for each SELL (``None`` for a BUY), replaying ``trades`` in order."""
    held: dict[str, Decimal] = {}
    out: dict[str, Decimal | None] = {}
    for t in trades:
        before = held.get(t.ticker, Decimal(0))
        if t.action == "BUY":
            held[t.ticker] = before + t.shares
            out[t.trade_id] = None
        else:
            out[t.trade_id] = min(Decimal(1), t.shares / before) if before > 0 else Decimal(1)
            held[t.ticker] = max(Decimal(0), before - t.shares)
    return out


def horizon_session(calendar: SessionCalendar, fill_date: date, days: int) -> date:
    """The last trading session on or before ``fill_date + days``."""
    day = fill_date + timedelta(days=days)
    for _ in range(_LOOKBACK_DAYS):
        if is_session(calendar, day):
            return day
        day -= timedelta(days=1)
    raise ValueError(f"no session within {_LOOKBACK_DAYS} days before {fill_date + timedelta(days=days)}")


def score_matured(
    trades: Sequence[Trade],
    existing: Sequence[OutcomeScore],
    *,
    benchmark: str,
    now: datetime,
    calendar: SessionCalendar,
    prices: PriceService,
    primary: PriceProvider,
    fallback: PriceProvider,
) -> list[OutcomeScore]:
    """New scores for every ``(trade, horizon)`` whose horizon session has completed and is not yet scored.

    Pairs the price seam cannot price, or prices only stale, are skipped and retried on the next call.
    """
    done = {s.key for s in existing}
    completed = last_completed_session(calendar, now)
    fractions = traded_fractions(trades)
    tables: dict[tuple[tuple[str, ...], date], PriceTable | None] = {}

    def table(tickers: tuple[str, ...], day: date) -> PriceTable | None:
        key = (tickers, day)
        if key not in tables:
            try:
                got: PriceTable | None = prices.valuation(list(tickers), day, primary, fallback)
            except (PriceError, ValuationUnavailable) as e:
                log.warning("outcomes: cannot price %s for %s: %s", tickers, day, e)
                got = None
            if got is not None and any(q.stale for q in got.quotes.values()):
                got = None  # a stale close is not a score basis; retry when a fresh one arrives
            tables[key] = got
        return tables[key]

    new: list[OutcomeScore] = []
    for t in trades:
        fill_date = t.timestamp.astimezone(PT).date()
        for days in HORIZONS:
            if (t.trade_id, days) in done:
                continue
            target = horizon_session(calendar, fill_date, days)
            if target > completed:
                continue
            end = table(tuple(dict.fromkeys((t.ticker, benchmark))), target)
            start = table((benchmark,), fill_date)
            if end is None or start is None:
                continue
            b0, b1 = start.quotes[benchmark].price, end.quotes[benchmark].price
            bench_return = q4((b1 - b0) / b0 * _HUNDRED)
            ticker_return = q4((end.quotes[t.ticker].price - t.fill_price) / t.fill_price * _HUNDRED)
            excess = q4(ticker_return - bench_return)
            fraction = fractions[t.trade_id]
            new.append(
                OutcomeScore(
                    trade_id=t.trade_id,
                    action=t.action,
                    ticker=t.ticker,
                    fill_date=fill_date,
                    horizon_days=days,
                    horizon_date=target,
                    fraction=q4(fraction) if fraction is not None else None,
                    traded_value=q4(t.cost),
                    ticker_return_pct=ticker_return,
                    benchmark=benchmark,
                    benchmark_return_pct=bench_return,
                    excess_pct=excess,
                    win=is_win(t.action, excess),
                    price_source=end.source,
                )
            )
    return new
