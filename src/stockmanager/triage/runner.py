"""The triage cron entry (v2 design D1, R7): its own single-flight job, independent of the tick and its lock.

Reads raw items from the fetch log (D8), applies the raw-text pre-filter inside the pipeline, and keeps a backlog
cursor so an item the classifier could not judge is retried on the next run (R4) while a judged one is not judged
twice. The cursor is an atomic file, so the backlog survives the event db being down.

It takes ``state/triage.lock`` and never ``state/tick.lock``: a slow classifier cannot starve ``deliver_pending`` or
execution, and the tick's watchdog and grace windows are unaffected (R7).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from stockmanager.config.triage import TriageConfig
from stockmanager.store import FileStore, fsutil
from stockmanager.triage.classifier import TriageClassifier
from stockmanager.triage.models import TriageItem
from stockmanager.triage.pipeline import TriagePipeline

log = logging.getLogger(__name__)

TRIAGE_LOCK_NAME = "triage.lock"
CURSOR_NAME = "triage_done.json"
MAX_CURSOR = 5000


@contextmanager
def triage_lock(root: Path) -> Iterator[bool]:
    """Hold ``state/triage.lock`` non-blocking for the ``with`` body; yields ``False`` if another run holds it."""
    lock_path = root / "state" / TRIAGE_LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def read_fetch_log(root: Path) -> list[TriageItem]:
    """Every row of ``fetch-log/*.jsonl`` as a ``TriageItem``, oldest file first. Malformed rows are skipped."""
    directory = root / "fetch-log"
    if not directory.exists():
        return []
    items: list[TriageItem] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                items.append(
                    TriageItem(
                        item_hash=str(row["item_hash"]),
                        url=str(row["url"]),
                        title=str(row["raw_title"]),
                        snippet=str(row["raw_snippet"]),
                        fetched_at=datetime.fromisoformat(row["fetched_at"]),
                        source=str(row.get("source", "")),
                    )
                )
            except (ValueError, KeyError, TypeError):
                continue
    return items


def _cursor_path(root: Path) -> Path:
    return root / "state" / CURSOR_NAME


def load_done(root: Path) -> list[str]:
    """Item hashes already judged, oldest first. A missing or torn cursor is empty (everything is re-judged once)."""
    try:
        raw = json.loads(_cursor_path(root).read_text(encoding="utf-8"))
        return [str(h) for h in raw]
    except (OSError, ValueError, TypeError):
        return []


def save_done(root: Path, done: list[str]) -> None:
    """Atomically write the cursor, keeping the newest ``MAX_CURSOR`` entries."""
    text = json.dumps(done[-MAX_CURSOR:], indent=2, ensure_ascii=False) + "\n"
    fsutil.atomic_write_text(_cursor_path(root), text)


def run_triage(store: FileStore, cfg: TriageConfig, classifier: TriageClassifier, held: set[str]) -> dict[str, int]:
    """One triage cron run; returns action counts. Does nothing (and says so) when ``triage.enabled`` is false."""
    if not cfg.enabled:
        return {"disabled": 1}
    with triage_lock(store.root) as acquired:
        if not acquired:
            return {"skipped_busy": 1}
        done = load_done(store.root)
        seen = set(done)
        backlog = [i for i in read_fetch_log(store.root) if i.item_hash not in seen][: cfg.max_items_per_run]
        result = TriagePipeline(store, cfg, classifier, held).run(backlog, store.clock())
        judged = [i.item_hash for i in backlog if i.item_hash not in result.stalled]
        save_done(store.root, [*done, *judged])
        counts: dict[str, int] = {}
        for d in result.decisions:
            counts[d.action] = counts.get(d.action, 0) + 1
        return counts
