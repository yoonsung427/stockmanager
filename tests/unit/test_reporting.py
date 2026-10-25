"""R13 reporting: the median covers human-acted alerts only; the auto-execution share is a separate number."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from stockmanager.reporting import AlertRecord, response_report

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
