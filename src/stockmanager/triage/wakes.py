"""The triage wake queue: how an escalation reaches the scheduler without touching it (v2 design D1, R7).

The triage cron job and the tick are separate processes, and ``state/scheduler.json`` is written lock-free by the
tick, so the triage job must not write it (its wake would be clobbered by the tick's next save). Instead each wake is
its own file, ``triage-wakes/<cycle_id>.json``, created atomically and never rewritten. The tick reads the directory
into ``SchedulerState.triage_wakes``, ``list_due_jobs`` turns the due ones into ``triage_escalation`` DueJobs, and the
tick records completion in ``SchedulerState.triage_done``.

Cycle-ID rule (collision avoidance): evaluation cycle ids are ISO dates (``2026-10-24``), and everything date-keyed
(``completed_slots``, ``cycles/``, ``eval-<id>`` alert ids) assumes that shape. A triage cycle id is
``triage-<PT %Y%m%dT%H%M%S>-<ticker>-<event_hash[:8]>``: it can never parse as a date, so it can never equal or
shadow a weekly slot, and triage completion is tracked in ``triage_done``, never in ``completed_slots``.

This module imports nothing from ``scheduler`` or ``execution``: the scheduler imports *it*.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from stockmanager.store import fsutil

log = logging.getLogger(__name__)

CYCLE_PREFIX = "triage-"
WAKES_DIR = "triage-wakes"


def cycle_id_for(created_at: datetime, ticker: str, event_hash: str) -> str:
    """The deterministic triage cycle id (see the module docstring for why it cannot collide with a date)."""
    return f"{CYCLE_PREFIX}{created_at:%Y%m%dT%H%M%S}-{ticker}-{event_hash[:8]}"


@dataclass(frozen=True)
class TriageWake:
    """One pending off-cycle evaluation request. ``due_at`` is the delivery moment, and the veto window's start."""

    cycle_id: str
    ticker: str
    event_hash: str
    source_url: str
    created_at: datetime
    due_at: datetime

    def to_json(self) -> str:
        """Canonical JSON (indent 2, trailing newline)."""
        payload = {
            "cycle_id": self.cycle_id,
            "ticker": self.ticker,
            "event_hash": self.event_hash,
            "source_url": self.source_url,
            "created_at": self.created_at.isoformat(),
            "due_at": self.due_at.isoformat(),
        }
        return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> TriageWake:
        """Inverse of ``to_json``; raises ``ValueError``/``KeyError``/``TypeError`` on a bad file."""
        data = json.loads(text)
        return cls(
            cycle_id=str(data["cycle_id"]),
            ticker=str(data["ticker"]),
            event_hash=str(data["event_hash"]),
            source_url=str(data["source_url"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            due_at=datetime.fromisoformat(data["due_at"]),
        )


class WakeQueue:
    """Directory of wake files under ``<root>/triage-wakes/``."""

    def __init__(self, root: Path) -> None:
        self._dir = root / WAKES_DIR

    def enqueue(self, wake: TriageWake) -> bool:
        """Create the wake file; return False if that cycle id already exists (idempotent, never rewrites)."""
        path = self._dir / f"{wake.cycle_id}.json"
        if path.exists():
            return False
        fsutil.atomic_write_text(path, wake.to_json())
        return True

    def all(self) -> list[TriageWake]:
        """Every readable wake, oldest ``due_at`` first. A torn or foreign file is skipped, never fatal."""
        if not self._dir.exists():
            return []
        out: list[TriageWake] = []
        for path in sorted(self._dir.glob(f"{CYCLE_PREFIX}*.json")):
            try:
                out.append(TriageWake.from_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("skipping unreadable triage wake %s", path.name)
        return sorted(out, key=lambda w: (w.due_at, w.cycle_id))

    def created_on(self, day: date) -> int:
        """How many wakes were created on PT calendar ``day`` (for ``max_escalations_per_day``)."""
        return sum(1 for w in self.all() if w.created_at.date() == day)
