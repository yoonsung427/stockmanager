"""R13 reporting: the median covers human-acted alerts only; the auto-execution share is a separate number."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from stockmanager.outcomes.models import OutcomeScore
from stockmanager.reporting import (
    AlertRecord,
    describe_noise,
    describe_trend,
    hit_rate_trend,
    noise_ratio,
    response_report,
    veto_rate_trend,
)
from tests.support.proposals import make_proposal

PT = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 26, 7, 0, tzinfo=PT)


def rec(alert_id: str, minutes: int | None, *, auto: bool = False) -> AlertRecord:
    return AlertRecord(alert_id, T0, None if minutes is None else T0 + timedelta(minutes=minutes), auto)


def test_median_is_over_human_acted_alerts_only() -> None:
    report = response_report([rec("a", 10), rec("b", 30), rec("c", 50), rec("auto", 0, auto=True), rec("open", None)])
    assert report.median_latency == timedelta(minutes=30)
    assert (report.total, report.human_acted, report.auto_executed) == (5, 3, 1)
    assert not report.pending


def test_even_count_averages_the_middle_pair() -> None:
    assert response_report([rec("a", 10), rec("b", 20)]).median_latency == timedelta(minutes=15)


def test_auto_executed_never_moves_the_median() -> None:
    base = response_report([rec("a", 10), rec("b", 30)])
    with_auto = response_report([rec("a", 10), rec("b", 30), rec("x", 0, auto=True), rec("y", 1, auto=True)])
    assert with_auto.median_latency == base.median_latency
    assert with_auto.auto_share == Decimal("0.5000")
    assert base.auto_share == Decimal("0.0000")


def test_auto_share_counts_acted_flag_even_if_an_action_ts_exists() -> None:
    report = response_report([rec("x", 5, auto=True), rec("a", 20)])
    assert (report.human_acted, report.auto_executed, report.median_latency) == (1, 1, timedelta(minutes=20))


def test_no_human_acted_alerts_is_pending_not_zero() -> None:
    report = response_report([rec("auto", 0, auto=True), rec("open", None)])
    assert report.median_latency is None and report.pending
    assert report.auto_share == Decimal("0.5000")


def test_no_alerts_at_all_is_pending_with_no_share() -> None:
    report = response_report([])
    assert report.pending and report.auto_share is None and report.total == 0


def test_action_before_delivery_is_left_out_of_the_median() -> None:
    report = response_report([rec("bad", -5), rec("a", 10)])
    assert report.median_latency == timedelta(minutes=10) and report.human_acted == 1


# -- M3 noise ratio ------------------------------------------------------------------------------------------------


def test_noise_ratio_is_action_required_over_all_alerts() -> None:
    report = noise_ratio(["alert", "info", "positive", "info", "alert"])
    assert (report.total, report.action_required, report.ratio) == (5, 2, Decimal("0.4000"))
    assert describe_noise(report) == "Noise ratio: 40.0000% (2 of 5 alerts needed action; target < 20%)."


def test_noise_ratio_with_no_alerts_is_none_not_zero() -> None:
    report = noise_ratio([])
    assert (report.total, report.ratio) == (0, None)
    assert describe_noise(report) == "Noise ratio: no alerts in the window."


def test_noise_ratio_reports_but_does_not_enforce_the_target() -> None:
    assert noise_ratio(["alert"] * 9 + ["info"]).ratio == Decimal("0.9000")
    assert noise_ratio(["info"] * 3).ratio == Decimal("0.0000")


# -- M4 alignment proxy --------------------------------------------------------------------------------------------


def prop(pid: str, day: int, *, vetoed: bool) -> object:
    return make_proposal(
        pid, state="VETOED" if vetoed else "PROPOSED", created_at=datetime(2026, 10, day, 7, tzinfo=PT)
    )


def test_veto_rate_trend_groups_by_created_date_and_compares_latest_to_previous() -> None:
    docs = [
        prop("P-1", 21, vetoed=True),
        prop("P-2", 21, vetoed=False),
        prop("P-3", 22, vetoed=True),
        prop("P-4", 22, vetoed=True),
        prop("P-5", 22, vetoed=False),
        prop("P-6", 22, vetoed=False),
    ]
    trend = veto_rate_trend(docs)  # type: ignore[arg-type]
    assert [(p.period.day, p.numerator, p.denominator, p.rate) for p in trend.series] == [
        (21, 1, 2, Decimal("0.5000")),
        (22, 2, 4, Decimal("0.5000")),
    ]
    assert trend.direction == "flat"
    docs.append(prop("P-7", 23, vetoed=True))
    up = veto_rate_trend(docs)  # type: ignore[arg-type]
    assert up.direction == "up"
    assert (
        describe_trend("Veto-rate", "proposals", up)
        == "Veto-rate trend: up, 100.0000% on 2026-10-23 (1 of 1) vs 50.0000% on 2026-10-22."
    )


def _score(trade_id: str, fill_day: int, *, win: bool, horizon: int = 90) -> OutcomeScore:
    return OutcomeScore(
        trade_id=trade_id,
        action="BUY",
        ticker="AAA",
        fill_date=date(2026, 7, fill_day),
        horizon_days=horizon,
        horizon_date=date(2026, 10, fill_day),
        fraction=None,
        traded_value=Decimal(100),
        ticker_return_pct=Decimal(1),
        benchmark="SPY",
        benchmark_return_pct=Decimal(0),
        excess_pct=Decimal(1),
        win=win,
        price_source="yahoo",
    )


def test_hit_rate_trend_uses_gate_horizon_scores_per_fill_date() -> None:
    scores = [
        _score("a", 1, win=True),
        _score("b", 1, win=False),
        _score("c", 8, win=False),
        _score("d", 8, win=False, horizon=30),  # never gates
    ]
    trend = hit_rate_trend(scores)
    assert [p.rate for p in trend.series] == [Decimal("0.5000"), Decimal("0.0000")]
    assert trend.direction == "down"
    assert describe_trend("Hit-rate", "scored trades", trend) == (
        "Hit-rate trend: down, 0.0000% on 2026-07-08 (0 of 1) vs 50.0000% on 2026-07-01."
    )


def test_single_period_gives_no_trend_direction() -> None:
    trend = veto_rate_trend([prop("P-1", 21, vetoed=True)])  # type: ignore[list-item]
    assert trend.direction is None
    line = describe_trend("Veto-rate", "proposals", trend)
    assert "no direction yet, only one period" in line and "up" not in line and "down" not in line
    assert hit_rate_trend([_score("a", 1, win=True)]).direction is None


def test_no_data_says_so() -> None:
    assert describe_trend("Hit-rate", "scored trades", hit_rate_trend([])) == "Hit-rate trend: no scored trades yet."
    assert veto_rate_trend([]).direction is None
