"""Reporting over stored records (v2 R13, M3, M4): pure functions, no disk, no clock."""

from __future__ import annotations

from stockmanager.reporting.reporting import (
    AlertRecord,
    NoiseReport,
    PeriodRate,
    ResponseReport,
    Trend,
    describe_noise,
    describe_trend,
    hit_rate_trend,
    noise_ratio,
    response_report,
    veto_rate_trend,
)

__all__ = [
    "AlertRecord",
    "NoiseReport",
    "PeriodRate",
    "ResponseReport",
    "Trend",
    "describe_noise",
    "describe_trend",
    "hit_rate_trend",
    "noise_ratio",
    "response_report",
    "veto_rate_trend",
]
