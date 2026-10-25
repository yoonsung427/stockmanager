"""Response-time reporting (v2 R13): how long the owner takes to act on an alert.

Pure over injected records, so tests pass fixtures directly. The caller derives them from the atomic-file store
(hash-chained proposal transitions and stored decision records), never from SQLite at read time (D7).

The median covers *human-acted* alerts only. An auto-executed alert is not a human response, so it never enters the
median; its share of all alerts is reported as a separate number. With no human-acted alert there is no median:
the report says so (``median_latency is None``, ``pending``) instead of reporting zero.

Weekly-digest metrics (v2 M3, M4), also pure over injected records:

* M3 noise ratio: alerts with severity ``"alert"`` (action-required) over all alerts. No alerts, no ratio.
* M4 alignment proxy: the veto-rate trend next to the hit-rate trend. LIMITATION: a ``ProposalDoc`` stores no
  ``cycle_id`` and no other stored record (transition ``cause``, fetch log, fact files) links a proposal to a
  cycle, so "per cycle" cannot be derived. Vetoes are grouped by the proposal's own ``created_at`` date, the only
  stored grouping; hit-rate is grouped by the scored trade's ``fill_date``. A trend needs two periods; with fewer
  the line says so and gives no direction.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from stockmanager.domain.docs import ProposalDoc
from stockmanager.domain.money import q4
from stockmanager.notify.outbox import Severity
from stockmanager.outcomes.models import GATE_HORIZON, OutcomeScore

#: M3 goal (not a gate): action-required alerts should stay under this share of all alerts.
NOISE_TARGET_PCT = 20

Direction = Literal["up", "down", "flat"]


@dataclass(frozen=True)
class AlertRecord:
    """One alert: when it was delivered, when (if ever) a person acted, and whether the system auto-executed it."""

    alert_id: str
    delivered_ts: datetime
    action_ts: datetime | None
    auto_executed: bool


@dataclass(frozen=True)
class ResponseReport:
    """R13 numbers. ``median_latency`` is ``None`` (pending) when no human-acted alert exists."""

    total: int
    human_acted: int
    auto_executed: int
    median_latency: timedelta | None
    auto_share: Decimal | None  # auto_executed / total, 4 dp; ``None`` when there are no alerts

    @property
    def pending(self) -> bool:
        """True when there is nothing to take a median over."""
        return self.median_latency is None


def response_report(records: Iterable[AlertRecord]) -> ResponseReport:
    """Median delivered->action latency over human-acted alerts, plus the auto-execution share as its own number.

    Human-acted means ``action_ts`` is set, the alert was not auto-executed, and the action is not before delivery
    (a record that claims so is inconsistent and is left out of the median rather than counted as a zero).
    """
    rows = list(records)
    auto = sum(1 for r in rows if r.auto_executed)
    latencies = sorted(
        r.action_ts - r.delivered_ts
        for r in rows
        if r.action_ts is not None and not r.auto_executed and r.action_ts >= r.delivered_ts
    )
    median: timedelta | None = None
    if latencies:
        mid = len(latencies) // 2
        median = latencies[mid] if len(latencies) % 2 else (latencies[mid - 1] + latencies[mid]) / 2
    share = q4(Decimal(auto) / Decimal(len(rows))) if rows else None
    return ResponseReport(len(rows), len(latencies), auto, median, share)


@dataclass(frozen=True)
class NoiseReport:
    """M3 numbers. ``ratio`` is ``None`` when there are no alerts."""

    total: int
    action_required: int
    ratio: Decimal | None  # action_required / total, 4 dp


def noise_ratio(severities: Iterable[Severity]) -> NoiseReport:
    """Action-required (severity ``"alert"``) alerts over all alerts; the caller derives severities from stored alerts."""
    rows = list(severities)
    action = sum(1 for sev in rows if sev == "alert")
    return NoiseReport(len(rows), action, q4(Decimal(action) / Decimal(len(rows))) if rows else None)


def describe_noise(report: NoiseReport) -> str:
    """One plain line for the digest. Reports the ratio against the target; it does not enforce it."""
    if report.ratio is None:
        return "Noise ratio: no alerts in the window."
    pct = q4(report.ratio * 100)
    return (
        f"Noise ratio: {pct}% ({report.action_required} of {report.total} alerts needed action; "
        f"target < {NOISE_TARGET_PCT}%)."
    )


@dataclass(frozen=True)
class PeriodRate:
    """``numerator`` of ``denominator`` in one dated period, as a 4 dp fraction."""

    period: date
    numerator: int
    denominator: int
    rate: Decimal


@dataclass(frozen=True)
class Trend:
    """Latest period against the one before it. ``direction`` is ``None`` unless there are two periods."""

    series: tuple[PeriodRate, ...]  # oldest first
    direction: Direction | None


def _series(counts: dict[date, tuple[int, int]]) -> tuple[PeriodRate, ...]:
    return tuple(
        PeriodRate(day, num, den, q4(Decimal(num) / Decimal(den))) for day, (num, den) in sorted(counts.items())
    )


def _trend(series: tuple[PeriodRate, ...]) -> Trend:
    if len(series) < 2:
        return Trend(series, None)
    last, prev = series[-1].rate, series[-2].rate
    return Trend(series, "up" if last > prev else "down" if last < prev else "flat")


def veto_rate_trend(proposals: Iterable[ProposalDoc]) -> Trend:
    """VETOED / all proposals, per ``created_at`` date (see the module docstring: no cycle link is stored)."""
    counts: dict[date, tuple[int, int]] = {}
    for doc in proposals:
        vetoed, total = counts.get(doc.created_at.date(), (0, 0))
        counts[doc.created_at.date()] = (vetoed + (doc.state == "VETOED"), total + 1)
    return _trend(_series(counts))


def hit_rate_trend(scores: Iterable[OutcomeScore]) -> Trend:
    """Wins / scored trades per ``fill_date``, over the gate horizon only (as the hit-rate itself is)."""
    counts: dict[date, tuple[int, int]] = {}
    for s in scores:
        if s.horizon_days != GATE_HORIZON:
            continue
        wins, total = counts.get(s.fill_date, (0, 0))
        counts[s.fill_date] = (wins + s.win, total + 1)
    return _trend(_series(counts))


def describe_trend(label: str, unit: str, trend: Trend) -> str:
    """One plain line per trend; with fewer than two periods it says so instead of naming a direction."""
    if not trend.series:
        return f"{label} trend: no {unit} yet."
    last = trend.series[-1]
    now = f"{last.rate * 100}% on {last.period} ({last.numerator} of {last.denominator})"
    if trend.direction is None:
        return f"{label} trend: no direction yet, only one period so far ({now})."
    prev = trend.series[-2]
    return f"{label} trend: {trend.direction}, {now} vs {prev.rate * 100}% on {prev.period}."
