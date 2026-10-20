"""Risk metrics (SS §8.3): ROI, drawdown, concentration, single-position risk, volatility, correlation."""

from __future__ import annotations

from decimal import Decimal

from stockmanager.domain.metrics import (
    benchmark_relative,
    correlation_matrix,
    flagged_correlations,
    max_drawdown,
    max_drawdown_alert,
    momentum,
    position_weights,
    roi,
    sector_concentration,
    sector_concentration_alert,
    sector_weights,
    sharpe,
    single_position_alert,
    single_position_risk,
    volatility,
)

# -- roi / benchmark_relative --------------------------------------------------------------------------------


def test_roi_basic() -> None:
    assert roi(Decimal(50000), Decimal(55000)) == Decimal(10)


def test_roi_none_for_non_positive_start() -> None:
    assert roi(Decimal(0), Decimal(100)) is None
    assert roi(Decimal(-5), Decimal(100)) is None


def test_benchmark_relative_requires_both() -> None:
    assert benchmark_relative(Decimal(10), Decimal(4)) == Decimal(6)
    assert benchmark_relative(None, Decimal(4)) is None
    assert benchmark_relative(Decimal(10), None) is None


# -- max_drawdown ---------------------------------------------------------------------------------------------


def test_max_drawdown_peak_to_trough() -> None:
    series = [Decimal(100), Decimal(120), Decimal(90), Decimal(110)]
    # peak 120, trough 90: (120-90)/120 = 25%
    assert max_drawdown(series) == Decimal(25)


def test_max_drawdown_none_for_single_point() -> None:
    assert max_drawdown([Decimal(100)]) is None
    assert max_drawdown([]) is None


def test_max_drawdown_alert_threshold() -> None:
    assert max_drawdown_alert(Decimal("5.0001")) is True
    assert max_drawdown_alert(Decimal(5)) is False  # strictly-greater-than (SS §8.3)
    assert max_drawdown_alert(None) is False


# -- volatility / sharpe ----------------------------------------------------------------------------------------


def test_volatility_none_for_too_little_data() -> None:
    assert volatility([Decimal(100)]) is None
    assert volatility([Decimal(100), Decimal(101)]) is None  # only one return observation


def test_volatility_is_positive_for_varying_returns() -> None:
    series = [Decimal(100), Decimal(105), Decimal(98), Decimal(110)]
    v = volatility(series)
    assert v is not None and v > 0


def test_volatility_biweekly_annualization_scales_up() -> None:
    series = [Decimal(100), Decimal(105), Decimal(98), Decimal(110)]
    plain = volatility(series)
    scaled = volatility(series, annualize_biweekly=True)
    assert plain is not None and scaled is not None
    assert scaled > plain


def test_sharpe_none_for_zero_stdev_or_too_little_data() -> None:
    assert sharpe([Decimal(100), Decimal(101)], Decimal(0)) is None
    assert sharpe([Decimal(100), Decimal(100), Decimal(100)], Decimal(0)) is None  # zero variance


def test_sharpe_positive_mean_excess_gives_positive_ratio() -> None:
    series = [Decimal(100), Decimal(105), Decimal(110), Decimal(118)]
    s = sharpe(series, Decimal("0.0"))
    assert s is not None and s > 0


# -- position_weights / single_position_risk --------------------------------------------------------------------


def test_position_weights_sum_to_100_including_cash() -> None:
    weights = position_weights({"AAPL": Decimal(6000), "MSFT": Decimal(3000)}, cash=Decimal(1000))
    assert sum(weights.values()) == Decimal(100)
    assert weights["cash"] == Decimal(10)


def test_position_weights_empty_for_non_positive_nav() -> None:
    assert position_weights({}, cash=Decimal(0)) == {}


def test_single_position_risk_is_the_largest_holding() -> None:
    weights = position_weights({"AAPL": Decimal(4000), "MSFT": Decimal(1000)}, cash=Decimal(5000))
    assert single_position_risk(weights) == Decimal(40)


def test_single_position_risk_none_with_no_positions() -> None:
    assert single_position_risk({"cash": Decimal(100)}) is None


def test_single_position_alert_threshold() -> None:
    assert single_position_alert(Decimal("40.0001")) is True
    assert single_position_alert(Decimal(40)) is False
    assert single_position_alert(None) is False


# -- sector_weights / sector_concentration ------------------------------------------------------------------


def test_sector_weights_groups_by_sector_and_unclassified_fallback() -> None:
    values = {"AAPL": Decimal(6000), "MSFT": Decimal(2000), "XOM": Decimal(2000)}
    sectors = {"AAPL": "Technology", "MSFT": "Technology"}
    weights = sector_weights(values, sectors, cash=Decimal(0))
    assert weights["Technology"] == Decimal(80)
    assert weights["Unclassified"] == Decimal(20)


def test_sector_concentration_is_the_max_sector() -> None:
    assert sector_concentration({"Technology": Decimal(80), "Energy": Decimal(20)}) == Decimal(80)
    assert sector_concentration({}) is None


def test_sector_concentration_alert_threshold() -> None:
    assert sector_concentration_alert(Decimal("80.0001")) is True
    assert sector_concentration_alert(Decimal(80)) is False
    assert sector_concentration_alert(None) is False


# -- correlation_matrix -----------------------------------------------------------------------------------------


def test_correlation_matrix_perfectly_correlated_series() -> None:
    a = [Decimal(100), Decimal(110), Decimal(121), Decimal("133.1")]
    b = [Decimal(50), Decimal(55), Decimal("60.5"), Decimal("66.55")]  # identical % returns
    matrix = correlation_matrix({"A": a, "B": b})
    corr = matrix[("A", "B")]
    assert corr is not None and corr > Decimal("0.99")


def test_correlation_matrix_none_for_too_little_data() -> None:
    matrix = correlation_matrix({"A": [Decimal(100), Decimal(101)], "B": [Decimal(50), Decimal(51)]})
    assert matrix[("A", "B")] is None


def test_flagged_correlations_threshold() -> None:
    matrix = {("A", "B"): Decimal("0.9"), ("A", "C"): Decimal("0.5"), ("B", "C"): None}
    flagged = flagged_correlations(matrix)
    assert flagged == [("A", "B", Decimal("0.9"))]


# -- momentum -------------------------------------------------------------------------------------------------


def test_momentum_percent_change_over_n_periods() -> None:
    series = [Decimal(100), Decimal(102), Decimal(105), Decimal(110)]
    assert momentum(series, 1) == (Decimal(110) - Decimal(105)) / Decimal(105) * 100
    assert momentum(series, 3) == (Decimal(110) - Decimal(100)) / Decimal(100) * 100


def test_momentum_none_for_too_short_series_or_non_positive_base() -> None:
    assert momentum([Decimal(100), Decimal(101)], 5) is None
    assert momentum([Decimal(0), Decimal(10)], 1) is None
