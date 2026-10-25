"""Decision log (v2 design D1/D7): every triage decision as a ``triage_decisions`` row in the SQLite event db.

SQLite is a replayable event log, never the source of truth (D7). A write failure is reported to the caller as
``False`` and never stops triage: dedupe and the wake queue live in the atomic-file store and keep working (R4).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from stockmanager.store import eventsdb
from stockmanager.triage.models import Decision

log = logging.getLogger(__name__)


class DecisionLog:
    """Appends ``Decision`` rows to ``<root>/state/events.sqlite``."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def record(self, decisions: Sequence[Decision]) -> bool:
        """Append ``decisions`` in one transaction; return False (and log) if the event db is unavailable."""
        if not decisions:
            return True
        rows = [
            (
                d.ts.isoformat(),
                d.ticker,
                d.event_hash,
                int(d.hard_gate),
                d.confidence,
                d.action,
                d.source_url,
            )
            for d in decisions
        ]
        try:
            with eventsdb.connect(self._root) as conn:
                conn.executemany(
                    "INSERT INTO triage_decisions (ts, ticker, event_hash, hard_gate, confidence, action, source_url)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
        except sqlite3.Error as e:
            log.error("triage decision log unavailable (%s); %d decisions not recorded", type(e).__name__, len(rows))
            return False
        return True

    def read(self) -> list[tuple[str, str, str, int, float | None, str, str | None]]:
        """Every row as ``(ts, ticker, event_hash, hard_gate, confidence, action, source_url)``. Raises ``sqlite3.Error``."""
        with eventsdb.connect(self._root) as conn:
            return list(
                conn.execute(
                    "SELECT ts, ticker, event_hash, hard_gate, confidence, action, source_url"
                    " FROM triage_decisions ORDER BY id"
                )
            )
