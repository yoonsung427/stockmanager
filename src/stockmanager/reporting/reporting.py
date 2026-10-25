"""Response-time reporting (v2 R13): how long the owner takes to act on an alert.

Pure over injected records, so tests pass fixtures directly. The caller derives them from the atomic-file store
(hash-chained proposal transitions and stored decision records), never from SQLite at read time (D7).

The median covers *human-acted* alerts only. An auto-executed alert is not a human response, so it never enters the
median; its share of all alerts is reported as a separate number. With no human-acted alert there is no median:
the report says so (``median_latency is None``, ``pending``) instead of reporting zero.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from stockmanager.domain.money import q4


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
