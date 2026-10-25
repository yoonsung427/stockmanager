"""Outcome scoring, hit-rate and the reasoner outcomes section (v2 O1-O3, rulings R8 and R12).

Prices come from a per-date double behind the ``PriceProvider`` port; nothing touches the network.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from stockmanager.domain.docs import PriceQuote, Trade
from stockmanager.domain.timeutil import PT
from stockmanager.evaluation.input_builder import _outcomes_section, _veto_history
from stockmanager.evaluation.prompts import load_reasoner_prompt
from stockmanager.evaluation.reasoner_io import (
    MAX_OUTCOME_RECORDS,
    BenchmarkSnapshot,
    OutcomeRecord,
    OutcomesSection,
    PortfolioSnapshot,
    ReasonerConfig,
    ReasonerInput,
    RiskMetrics,
)
from stockmanager.outcomes.models import HORIZONS, MIN_SCORED, OutcomeScore, hit_rate, is_win
from stockmanager.outcomes.scoring import dump_scores, load_scores, score_matured, scores_path, traded_fractions
from stockmanager.pricing.errors import PriceError
from stockmanager.pricing.service import PriceService
from stockmanager.sim.calendar import FakeCalendar
from stockmanager.store import FileStore
from tests.support.proposals import advance, make_proposal

FILL = datetime(2026, 1, 5, 6, 30, tzinfo=PT)  # Monday; +30d Wed Feb 4, +60d Fri Mar 6, +90d Sun Apr 5 -> Fri Apr 3
DAYS = {30: date(2026, 2, 4), 60: date(2026, 3, 6), 90: date(2026, 4, 3)}
LATE = datetime(2026, 4, 6, 14, 0, tzinfo=PT)
MID = datetime(2026, 3, 10, 14, 0, tzinfo=PT)  # 30 and 60 matured, 90 not


class DatedProvider:
    """A ``PriceProvider`` answering from ``(ticker, day) -> price``; an absent key is a ``missing`` price error."""

    name: Literal["yahoo", "finnhub"] = "yahoo"

    def __init__(self, prices: dict[tuple[str, date], Decimal]) -> None:
        self.prices = prices

    def get_price(self, ticker: str, session_date: date, kind: Literal["open", "close"]) -> PriceQuote:
        price = self.prices.get((ticker, session_date))
        if price is None:
            raise PriceError("missing", ticker, "no scripted price")
        return PriceQuote(ticker, session_date, kind, price, self.name, LATE)

    def prime(self, tickers: object, session_date: date, kind: Literal["open", "close"]) -> dict[str, Exception]:
        return {}


def trade(
    trade_id: str, action: Literal["BUY", "SELL"], ticker: str, shares: str, price: str, at: datetime = FILL
) -> Trade:
    return Trade(
        trade_id=trade_id,
        timestamp=at,
        action=action,
        ticker=ticker,
        shares=Decimal(shares),
        fill_price=Decimal(price),
        cost=Decimal(shares) * Decimal(price),
        price_source="yahoo",
    )


def world(ticker_end: str, bench_end: str, bench_start: str = "400") -> DatedProvider:
    prices = {("SPY", FILL.date()): Decimal(bench_start)}
    for day in DAYS.values():
        prices[("AAA", day)] = Decimal(ticker_end)
        prices[("SPY", day)] = Decimal(bench_end)
    return DatedProvider(prices)


def score(
    trades: list[Trade],
    provider: DatedProvider,
    now: datetime = LATE,
    existing: list[OutcomeScore] | None = None,
) -> list[OutcomeScore]:
    return score_matured(
        trades,
        existing or [],
        benchmark="SPY",
        now=now,
        calendar=FakeCalendar(),
        prices=PriceService(),
        primary=provider,
        fallback=provider,
    )


# -- O1 --------------------------------------------------------------------------------------------------------------


def test_buy_wins_when_ticker_beats_benchmark() -> None:  # O1 / R8
    got = score([trade("P-1-a", "BUY", "AAA", "10", "100")], world("120", "420"))
    assert {s.horizon_days for s in got} == set(HORIZONS)
    s = got[0]
    assert (s.ticker_return_pct, s.benchmark_return_pct, s.excess_pct) == (Decimal(20), Decimal(5), Decimal(15))
    assert s.win and s.fraction is None


def test_sell_direction_inverts_win_when_benchmark_outperformed() -> None:  # R8
    sold = trade("P-1-a", "SELL", "AAA", "5", "100")
    assert all(s.win for s in score([sold], world("110", "460")))  # sold shares +10%, benchmark +15%
    assert not any(s.win for s in score([sold], world("130", "460")))  # sold shares kept running: a loss


def test_tie_is_not_a_win() -> None:
    assert not is_win("BUY", Decimal(0)) and not is_win("SELL", Decimal(0))


def test_trim_is_scored_on_the_trimmed_fraction() -> None:  # R8
    trades = [trade("P-1-a", "BUY", "AAA", "10", "100"), trade("P-2-a", "SELL", "AAA", "4", "100")]
    assert traded_fractions(trades) == {"P-1-a": None, "P-2-a": Decimal("0.4")}
    sell = next(s for s in score(trades, world("110", "440")) if s.trade_id == "P-2-a")
    assert sell.fraction == Decimal("0.4000") and sell.traded_value == Decimal("400.0000")


def test_only_matured_horizons_are_scored_and_retry_fills_the_rest() -> None:  # O1
    t = [trade("P-1-a", "BUY", "AAA", "10", "100")]
    first = score(t, world("120", "420"), now=MID)
    assert sorted(s.horizon_days for s in first) == [30, 60]
    later = score(t, world("120", "420"), existing=first)
    assert [s.horizon_days for s in later] == [90]


def test_price_failure_leaves_pair_unscored_until_it_can_be_priced() -> None:  # O1
    provider = world("120", "420")
    del provider.prices[("AAA", DAYS[90])]
    t = [trade("P-1-a", "BUY", "AAA", "10", "100")]
    assert sorted(s.horizon_days for s in score(t, provider)) == [30, 60]
    provider.prices[("AAA", DAYS[90])] = Decimal(120)
    assert 90 in {s.horizon_days for s in score(t, provider)}


def test_every_executed_fill_is_scored_at_every_matured_horizon() -> None:  # O1 gate: zero unscored while live
    trades = [trade("P-1-a", "BUY", "AAA", "10", "100"), trade("P-2-a", "SELL", "AAA", "4", "100")]
    got = score(trades, world("120", "420"))
    assert {s.key for s in got} == {(t.trade_id, h) for t in trades for h in HORIZONS}
    assert score(trades, world("120", "420"), existing=got) == []  # idempotent


def test_scores_round_trip_through_the_evaluations_file(tmp_path: Path) -> None:
    got = score([trade("P-1-a", "BUY", "AAA", "10", "100")], world("120", "420"))
    path = scores_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(dump_scores(got), encoding="utf-8")
    assert load_scores(tmp_path) == sorted(got, key=lambda s: (s.fill_date, s.trade_id, s.horizon_days))
    assert path.suffix == ".jsonl"  # the dashboard globs evaluations/*.json; this file must stay out of it


# -- O2 --------------------------------------------------------------------------------------------------------------


def _scored(n: int, wins: int, horizon: int = 90) -> list[OutcomeScore]:
    base = score([trade("P-0-a", "BUY", "AAA", "10", "100")], world("120", "420"))[0]
    return [
        base.model_copy(update={"trade_id": f"P-{i}-a", "horizon_days": horizon, "win": i < wins}) for i in range(n)
    ]


def test_hit_rate_is_provisional_below_n_and_reports_counts_only() -> None:  # R12
    rate = hit_rate(_scored(MIN_SCORED - 1, 5))
    assert rate.provisional and rate.hit_rate_pct is None and (rate.wins, rate.scored) == (5, MIN_SCORED - 1)


def test_hit_rate_flips_at_n_without_a_human_step() -> None:  # R12
    rate = hit_rate(_scored(MIN_SCORED, 6))
    assert not rate.provisional and rate.hit_rate_pct == Decimal("60.0000")


def test_30_and_60_day_scores_never_gate_the_hit_rate() -> None:  # R8
    rate = hit_rate(_scored(MIN_SCORED, 10, horizon=30) + _scored(MIN_SCORED, 10, horizon=60))
    assert rate.provisional and rate.scored == 0
    assert [(t.horizon_days, t.scored) for t in rate.trends] == [(30, MIN_SCORED), (60, MIN_SCORED), (90, 0)]


# -- O3 --------------------------------------------------------------------------------------------------------------


def _store_with(tmp_path: Path, scores: list[OutcomeScore]) -> FileStore:
    store = FileStore(tmp_path)
    scores_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    scores_path(tmp_path).write_text(dump_scores(scores), encoding="utf-8")
    return store


def test_section_below_n_is_provisional_records_without_verdicts(tmp_path: Path) -> None:
    section = _outcomes_section(_store_with(tmp_path, _scored(MIN_SCORED - 1, 5)))
    assert section.provisional and section.stats is None
    assert section.records and all(r.win is None for r in section.records)
    assert all(r.ordinal.startswith("O") and "P-" not in r.ordinal for r in section.records)


def test_section_auto_flips_to_stats_at_n(tmp_path: Path) -> None:  # R12
    store = _store_with(tmp_path, _scored(MIN_SCORED - 1, 6))
    assert _outcomes_section(store).stats is None
    store = _store_with(tmp_path, _scored(MIN_SCORED, 6))
    section = _outcomes_section(store)
    assert not section.provisional and section.stats is not None
    assert (section.stats.scored, section.stats.wins, section.stats.hit_rate_pct) == (MIN_SCORED, 6, "60.0000")


def test_section_records_are_capped(tmp_path: Path) -> None:
    section = _outcomes_section(_store_with(tmp_path, _scored(MIN_SCORED * 3, 10)))
    assert len(section.records) == MAX_OUTCOME_RECORDS
    with pytest.raises(ValidationError):
        OutcomesSection(records=[section.records[0]] * (MAX_OUTCOME_RECORDS + 1))


def _reasoner_input(section: OutcomesSection) -> ReasonerInput:
    return ReasonerInput(
        cycle_id="2026-04-06",
        valuation_date="2026-04-03",
        price_source="yahoo",
        config=ReasonerConfig(
            benchmark="SPY", approval_mode="explicit", veto_window_hours=24, evaluation_frequency="weekly"
        ),
        portfolio=PortfolioSnapshot(nav="1000", cash="1000", cash_pct="100", positions=[]),
        benchmark=BenchmarkSnapshot(ticker="SPY", roi_pct=None, portfolio_roi_pct=None, relative_pct=None),
        metrics=RiskMetrics(
            max_drawdown_pct=None, volatility_weekly_pct=None, flagged_correlations=[], alerts_active=[]
        ),
        sector_weights=[],
        sector_etfs=[],
        facts=[],
        veto_history=[],
        outcomes=section,
    )


def test_rendered_prompt_carries_the_outcomes_block_as_quarantined_data(tmp_path: Path) -> None:
    section = _outcomes_section(_store_with(tmp_path, _scored(MIN_SCORED, 6)))
    text = load_reasoner_prompt().render_user(_reasoner_input(section))
    assert text.count("<outcomes>") == 1 and text.count("</outcomes>") == 1
    assert text.index("</recent_vetoes>") < text.index("<outcomes>") < text.index("Assess every holding")
    assert '"hit_rate_pct": "60.0000"' in text
    assert "P-0-a" not in text  # no trade or proposal ID reaches the model


def test_rendered_prompt_without_scores_has_an_empty_provisional_block() -> None:
    text = load_reasoner_prompt().render_user(_reasoner_input(OutcomesSection()))
    assert '"provisional": true' in text and '"records": []' in text


def test_veto_history_excludes_system_evaluation_supersedes(tmp_path: Path) -> None:  # O2 / design §4
    store = FileStore(tmp_path)
    at = datetime(2026, 10, 12, 8, 0, tzinfo=PT)
    owner = advance(make_proposal("P-2026-10-12-01"), "VETOED", at, actor="discord:owner", cause="vetoed")
    superseded = advance(
        make_proposal("P-2026-10-12-02"), "VETOED", at, actor="system:evaluation", cause="superseded: cycle re-ran"
    )
    for doc in (owner, superseded):
        store.save_proposal(doc.proposal_id, doc)
    history = _veto_history(store)
    assert len(history) == 1 and history[0].reason is None


def test_record_model_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        OutcomeRecord.model_validate({"ordinal": "O1", "action": "BUY", "ticker": "A", "thesis": "x"})
