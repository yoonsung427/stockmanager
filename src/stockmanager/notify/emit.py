"""Best-effort alert emission for code paths that hold no transaction (AS §9.2).

A skipped run (the lock was busy) or a halted one (the books diverge) has, by definition, no commit to ride on, so
these alerts are written straight to ``alerts/pending/`` through ``enqueue``, which dedupes against what is already
pending. An I/O failure here must never mask the outcome the caller is about to return, hence the swallowed
``OSError``: the next tick retries the job and raises the alert again.
"""

from __future__ import annotations

from collections.abc import Sequence

from stockmanager.notify.outbox import Alert, enqueue
from stockmanager.notify.templates import reconcile_divergence, run_skipped_busy
from stockmanager.store import FileStore

MAX_SIGNATURE_CHARS = 200


def _emit(alert: Alert, store: FileStore) -> None:
    try:
        enqueue(alert, store)
    except OSError:
        pass  # best-effort: the job re-runs next tick and re-raises the alert


def emit_run_skipped_busy(store: FileStore, job: str, slot: str) -> None:
    """``job`` found ``portfolio.lock`` held and gave up on ``slot`` (TS WP-02)."""
    _emit(run_skipped_busy(job, slot), store)


def emit_reconcile_divergence(store: FileStore, divergences: Sequence[str]) -> None:
    """A job halted because the books disagree with themselves (TS NN-26 / NN-41)."""
    _emit(reconcile_divergence("; ".join(divergences)[:MAX_SIGNATURE_CHARS]), store)
