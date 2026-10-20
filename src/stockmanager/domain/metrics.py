"""Risk metrics (AS §1.2, SS §8.3): pure functions over plain data.

``Decimal`` in; ``float`` is used only inside ``statistics`` (P10). Sample standard deviation (``n-1``). Weekly
returns are what daily series are resampled to before ``volatility``/``sharpe``; a ``biweekly`` series is annualised
with ``sqrt(26)``. Weights are of total NAV (positions + cash); cash is never counted as a sector. Alerts fire on
strictly-greater-than. Data too thin to compute a metric returns ``None`` -- never ``0``, ``inf`` or ``NaN``.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Mapping, Sequence
from decimal import Decimal

HUNDRED = Decimal(100)

#: SS §8.3 alert thresholds.
MAX_DRAWDOWN_ALERT_PCT = Decimal(5)
SECTOR_CONCENTRATION_ALERT_PCT = Decimal(80)
SINGLE_POSITION_ALERT_PCT = Decimal(40)
CORRELATION_ALERT = Decimal("0.8")


def roi(start_cash: Decimal, current_value: Decimal) -> Decimal | None:
    """Portfolio ROI % = ``(current_value - start_cash) / start_cash`` (SS §8.3). ``None`` if ``start_cash <= 0``."""
    if start_cash <= 0:
        return None
    return (current_value - start_cash) / start_cash * HUNDRED


def benchmark_relative(portfolio_roi: Decimal | None, benchmark_roi: Decimal | None) -> Decimal | None:
    """Portfolio ROI minus benchmark ROI over the same window (SS §8.3). ``None`` unless both are present."""
    if portfolio_roi is None or benchmark_roi is None:
        return None
    return portfolio_roi - benchmark_roi


def max_drawdown(nav_series: Sequence[Decimal]) -> Decimal | None:
    """Largest peak-to-trough decline (%) since inception (SS §8.3). ``None`` for fewer than 2 points."""
    if len(nav_series) < 2:
        return None
    peak = nav_series[0]
    worst = Decimal(0)
    for v in nav_series:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak * HUNDRED)
    return worst


def max_drawdown_alert(drawdown_pct: Decimal | None, threshold: Decimal = MAX_DRAWDOWN_ALERT_PCT) -> bool:
    """True iff ``drawdown_pct`` strictly exceeds ``threshold`` (SS §8.3: alert at >5%)."""
    return drawdown_pct is not None and drawdown_pct > threshold


def _period_returns(series: Sequence[Decimal]) -> list[float]:
    """Simple period-over-period returns as ``float``, skipping any non-positive starting point."""
    out: list[float] = []
    for a, b in itertools.pairwise(series):
        if a > 0:
            out.append(float((b - a) / a))
    return out


def _resample_weekly(daily_series: Sequence[Decimal], points_per_week: int = 5) -> list[Decimal]:
    """Take every ``points_per_week``-th point (and the last) of a daily series as a weekly series (TS MET-12)."""
    if not daily_series:
        return []
    weekly = list(daily_series[points_per_week - 1 :: points_per_week])
    if not weekly or weekly[-1] != daily_series[-1]:
        weekly.append(daily_series[-1])
    return weekly


def volatility(weekly_series: Sequence[Decimal], *, annualize_biweekly: bool = False) -> Decimal | None:
    """Standard deviation (%) of weekly returns (SS §8.3), sample stdev (``n-1``). ``None`` if fewer than 3 points.

    When the series is itself biweekly, pass ``annualize_biweekly=True`` to scale by ``sqrt(26)`` (TS MET-13).
    """
    rets = _period_returns(weekly_series)
    if len(rets) < 2:
        return None
    sd = statistics.stdev(rets)
    if annualize_biweekly:
        sd *= 26**0.5
    return Decimal(str(sd * 100))


def sharpe(weekly_series: Sequence[Decimal], risk_free_rate_annual: Decimal, *, periods_per_year: int = 52) -> Decimal | None:
    """Annualised Sharpe ratio from weekly returns; sample stdev (``n-1``). ``None`` if data-poor or stdev is zero."""
    rets = _period_returns(weekly_series)
    if len(rets) < 2:
        return None
    sd = statistics.stdev(rets)
    if sd == 0:
        return None
    mean = statistics.fmean(rets)
    rf_period = float(risk_free_rate_annual) / periods_per_year
    return Decimal(str(((mean - rf_period) / sd) * (periods_per_year**0.5)))


def position_weights(values: Mapping[str, Decimal], cash: Decimal) -> dict[str, Decimal]:
    """Weight (%) of NAV for each ticker, plus ``\"cash\"``; weights sum to 100 (TS DASH rule). ``{}`` if NAV <= 0."""
    nav = cash + sum(values.values(), Decimal(0))
    if nav <= 0:
        return {}
    out = {ticker: v / nav * HUNDRED for ticker, v in values.items()}
    out["cash"] = cash / nav * HUNDRED
    return out


def single_position_risk(weights_pct: Mapping[str, Decimal]) -> Decimal | None:
    """Largest position's weight (%), cash excluded (SS §8.3). ``None`` if there are no positions."""
    held = {t: w for t, w in weights_pct.items() if t != "cash"}
    return max(held.values()) if held else None


def single_position_alert(largest_pct: Decimal | None, threshold: Decimal = SINGLE_POSITION_ALERT_PCT) -> bool:
    """True iff the largest position strictly exceeds ``threshold`` (SS §8.3: alert at >40%, ``position_alert_pct``)."""
    return largest_pct is not None and largest_pct > threshold


def sector_weights(values: Mapping[str, Decimal], sectors: Mapping[str, str], cash: Decimal) -> dict[str, Decimal]:
    """Weight (%) of NAV per sector (cash excluded; unmapped tickers fall into ``\"Unclassified\"``, TS §10.2)."""
    nav = cash + sum(values.values(), Decimal(0))
    if nav <= 0:
        return {}
    totals: dict[str, Decimal] = {}
    for ticker, v in values.items():
        sector = sectors.get(ticker, "Unclassified")
        totals[sector] = totals.get(sector, Decimal(0)) + v
    return {sector: v / nav * HUNDRED for sector, v in totals.items()}


def sector_concentration(sector_weights_pct: Mapping[str, Decimal]) -> Decimal | None:
    """Max sector weight (%) across holdings (SS §8.3). ``None`` if there are no sectors."""
    return max(sector_weights_pct.values()) if sector_weights_pct else None


def sector_concentration_alert(max_sector_pct: Decimal | None, threshold: Decimal = SECTOR_CONCENTRATION_ALERT_PCT) -> bool:
    """True iff the largest sector weight strictly exceeds ``threshold`` (SS §8.3: alert at >80%, ``max_sector_pct``)."""
    return max_sector_pct is not None and max_sector_pct > threshold


def correlation_matrix(return_series: Mapping[str, Sequence[Decimal]]) -> dict[tuple[str, str], Decimal | None]:
    """Pairwise Pearson correlation of weekly returns for every pair of tickers (SS §8.3).

    Keys are ``(a, b)`` with ``a < b`` lexicographically. A pair is ``None`` when fewer than 3 overlapping return
    observations are available (too little data), never ``0``/``NaN``.
    """
    out: dict[tuple[str, str], Decimal | None] = {}
    tickers = sorted(return_series)
    for i, a in enumerate(tickers):
        for b in tickers[i + 1 :]:
            ra, rb = _period_returns(return_series[a]), _period_returns(return_series[b])
            n = min(len(ra), len(rb))
            if n < 3:
                out[(a, b)] = None
                continue
            try:
                corr = statistics.correlation(ra[-n:], rb[-n:])
            except statistics.StatisticsError:
                out[(a, b)] = None
                continue
            out[(a, b)] = None if math.isnan(corr) else Decimal(str(corr))
    return out


def flagged_correlations(
    matrix: Mapping[tuple[str, str], Decimal | None], threshold: Decimal = CORRELATION_ALERT
) -> list[tuple[str, str, Decimal]]:
    """Pairs whose correlation strictly exceeds ``threshold`` (SS §8.3: flagged if any pair >0.8)."""
    return [(a, b, corr) for (a, b), corr in matrix.items() if corr is not None and corr > threshold]


def momentum(series: Sequence[Decimal], periods: int) -> Decimal | None:
    """Percent change over the last ``periods`` observations of ``series``. ``None`` if the series is too short."""
    if periods <= 0 or len(series) <= periods:
        return None
    base = series[-1 - periods]
    if base <= 0:
        return None
    return (series[-1] - base) / base * HUNDRED
