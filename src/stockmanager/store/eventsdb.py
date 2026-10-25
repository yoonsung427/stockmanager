"""``workspace/state/events.sqlite`` (v2 design D7): a replayable event log, never the source of truth.

Only the ``triage_decisions`` table exists so far. A corrupt or unavailable database must not halt fills or
escalation bookkeeping: callers treat ``sqlite3.Error`` as "log unavailable" (``EventsDb`` itself never swallows it).
Dedupe and the proposal chain stay in the atomic-file store (R4).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: Actions that wake the evaluation path. A row with one of these and no ``source_url`` is rejected by the database
#: itself, behind the check in ``triage.models.Decision`` (v2 A5: no citation, no escalation).
ESCALATING_ACTIONS = ("escalate", "defer_digest")

SCHEMA = """
CREATE TABLE IF NOT EXISTS triage_decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    event_hash  TEXT NOT NULL,
    hard_gate   INTEGER NOT NULL CHECK (hard_gate IN (0, 1)),
    confidence  REAL,
    action      TEXT NOT NULL,
    source_url  TEXT,
    CHECK (action NOT IN ('escalate', 'defer_digest') OR (source_url IS NOT NULL AND source_url <> ''))
)
"""


def db_path(root: Path) -> Path:
    """``<root>/state/events.sqlite``."""
    return root / "state" / "events.sqlite"


@contextmanager
def connect(root: Path) -> Iterator[sqlite3.Connection]:
    """Open the event database (WAL), ensure the schema, commit on success, always close. Raises ``sqlite3.Error``."""
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()
