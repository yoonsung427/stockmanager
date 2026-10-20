"""``Transaction``: the only way to mutate the books (AS §8.5, SS §7.1 steps 2-5).

Holds ``portfolio.lock`` for the whole ``with`` block. Nothing is written until ``__exit__``, which commits in the
fixed order below. Any exception skips the remaining writes and still releases the lock.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

from pydantic import BaseModel

from stockmanager.domain.docs import PortfolioDoc, Status
from stockmanager.store import FileStore
from stockmanager.store.lock import Lock
from stockmanager.store.reconcile import (
    LogComparison,
    ReconcileError,
    compare_log,
    format_log_line,
    read_log_lines,
    reconcile_portfolio,
    repair_log_suffix,
)


def _dedupe_key(alert_text: str) -> str | None:
    """The ``dedupe_key`` of a serialised alert, or ``None`` if it has none or is not an alert document."""
    try:
        key = json.loads(alert_text).get("dedupe_key")
    except (ValueError, AttributeError):
        return None
    return key if isinstance(key, str) else None


class GateError(Exception):
    """The portfolio is not in the status the job requires (gate 2, evaluated under the lock)."""


class ReconcileHalt(Exception):
    """The books disagree with themselves; the job must stop (SS §7.1 step 6)."""

    def __init__(self, divergences: list[str]) -> None:
        super().__init__("; ".join(divergences))
        self.divergences = divergences


class TransactionError(Exception):
    """Misuse of a transaction, or a staged document that violates a books invariant."""


@dataclass(frozen=True)
class RunNote:
    """A run-notes file: ``name`` is relative to ``run-notes/``."""

    name: str
    text: str


class Transaction:
    """Context manager; build with ``FileStore.transaction``."""

    def __init__(self, store: FileStore, job: str, *, max_attempts: int = 3, sleep_seconds: float = 10.0) -> None:
        self._store = store
        self.job = job
        self._max_attempts = max_attempts
        self._sleep_seconds = sleep_seconds
        self._lock: Lock | None = None
        self._state: PortfolioDoc | None = None
        self._portfolio: PortfolioDoc | None = None
        self._log: list[str] = []
        self._proposals: dict[str, BaseModel | Mapping[str, Any]] = {}
        self._nav_rows: list[dict[str, Any]] = []
        self._evals: dict[str, str] = {}
        self._acks: list[Mapping[str, Any]] = []
        self._run_note: RunNote | None = None
        self._dashboard: dict[str, str] = {}
        self._alerts: dict[str, str] = {}
        self.repaired_log_ids: list[str] = []

    # -- lifecycle -----------------------------------------------------------------------------------------------

    def __enter__(self) -> Self:
        self._lock = self._store.lock.acquire(self.job, self._max_attempts, self._sleep_seconds)
        try:
            self._store.cleanup_tmp()
            if self._store.portfolio_exists():
                self._state = self._store.load_portfolio()  # re-read under the lock: the only state jobs may use
        except BaseException:
            self._release()
            raise
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        try:
            if exc_type is None:
                self._commit()
        finally:
            self._release()

    def _release(self) -> None:
        if self._lock is not None:
            lock, self._lock = self._lock, None
            self._store.lock.release(lock.token)

    # -- reads and gates -----------------------------------------------------------------------------------------

    @property
    def state(self) -> PortfolioDoc:
        """``portfolio.json`` as read under the lock."""
        if self._state is None:
            raise TransactionError("portfolio.json does not exist")
        return self._state

    def require(self, *, status: Status | Sequence[Status]) -> None:
        """Gate: raise ``GateError`` unless the portfolio is in (one of) the expected status(es)."""
        allowed = (status,) if isinstance(status, str) else tuple(status)
        actual = self._state.status if self._state else None
        if actual not in allowed:
            raise GateError(f"requires status {'|'.join(allowed)}, portfolio is {actual or 'missing'}")

    def reconcile(self) -> PortfolioDoc:
        """Authoritative reconcile under the lock (AS §1.6).

        Raises ``ReconcileHalt`` on any divergence or unreplayable history. A missing ``trades.log`` suffix is the
        one auto-repair: the lines are appended and recorded in ``repaired_log_ids``. Returns ``positions[]`` and
        ``cash`` as rebuilt from ``trades[]``; nothing is staged.
        """
        rebuilt, divergences = reconcile_portfolio(self.state)
        comparison: LogComparison = compare_log(self.state.trades, read_log_lines(self._store.trades_log_path))
        divergences += comparison.divergences
        if divergences:
            raise ReconcileHalt(divergences)
        if comparison.missing_suffix:
            expected = [format_log_line(t) for t in self.state.trades]
            try:
                repair_log_suffix(self._store.trades_log_path, expected)
            except ReconcileError as e:  # pragma: no cover - compare_log already proved prefix-ness
                raise ReconcileHalt([str(e)]) from e
            self.repaired_log_ids = comparison.missing_suffix
            self._note_log_repair(self.repaired_log_ids)
        return rebuilt

    def _note_log_repair(self, trade_ids: list[str]) -> None:
        """Record the ``trades.log`` auto-repair in ``run-notes/`` (TS NN-22).

        Written at once rather than at commit: the repair has already changed ``trades.log``, and the job may still
        raise before it commits, which must not leave an unexplained append behind.
        """
        now = self._store.clock()
        slug = "".join(c if c.isalnum() else "-" for c in self.job)
        body = (
            "---\n"
            f"job: {self.job}\n"
            f"started: {now.isoformat()}\n"
            f"ended: {now.isoformat()}\n"
            "outcome: log_repaired\n"
            "---\n\n"
            f"Repaired `trades.log`: appended the {len(trade_ids)} line(s) missing behind `portfolio.json` "
            f"({', '.join(trade_ids)}). The books were ahead of the log after an interrupted run; no trade was replayed.\n"
        )
        self._store.save_extras(run_notes={f"{now.strftime('%Y%m%dT%H%M%S')}-logrepair-{slug}.md": body})

    # -- staging (nothing touches disk) --------------------------------------------------------------------------

    def stage_portfolio(self, doc: PortfolioDoc) -> PortfolioDoc:
        """Stage the new books with ``rev + 1`` and ``updated_at``; ``trades[]`` must only grow."""
        old = self._state.trades if self._state else []
        if doc.trades[: len(old)] != old:
            raise TransactionError("trades[] is append-only: existing trades were changed or removed")
        staged = doc.model_copy(
            update={"rev": (self._state.rev if self._state else 0) + 1, "updated_at": self._store.clock()}
        )
        self._portfolio = staged
        return staged

    def stage_log(self, lines: list[str]) -> None:
        """Stage ``trades.log`` lines."""
        self._log.extend(lines)

    def stage_proposals(self, changed: Mapping[str, BaseModel | Mapping[str, Any]]) -> None:
        """Stage proposal files by ``proposal_id``."""
        self._proposals.update(changed)

    def stage_extras(
        self,
        nav_rows: Sequence[dict[str, Any]] = (),
        eval_docs: Mapping[str, str] | None = None,
        acks: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """Stage NAV rows (appended), evaluation files (name -> text) and acks (appended)."""
        self._nav_rows.extend(nav_rows)
        self._evals.update(eval_docs or {})
        self._acks.extend(acks)

    def stage_run_note(self, note: RunNote) -> None:
        """Stage the run-notes file."""
        self._run_note = note

    def stage_dashboard(self, bundle: Mapping[str, str]) -> None:
        """Stage dashboard files (name -> text)."""
        self._dashboard.update(bundle)

    def stage_alerts(self, alerts: Mapping[str, str]) -> None:
        """Stage alert-outbox files (name -> text) for ``alerts/pending/``.

        An alert whose ``dedupe_key`` matches one already pending on disk, or one staged earlier in this
        transaction, is dropped (AS §9.2). Files that carry no ``dedupe_key`` are always staged.
        """
        from stockmanager.notify.outbox import pending_dedupe_keys  # local import: outbox imports the store

        seen = pending_dedupe_keys(self._store.root)
        seen.update(k for k in map(_dedupe_key, self._alerts.values()) if k is not None)
        for name, text in alerts.items():
            key = _dedupe_key(text)
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            self._alerts[name] = text

    # -- commit --------------------------------------------------------------------------------------------------

    def _commit(self) -> None:
        """Write in the fixed order of AS §8.5. The first exception stops all later writes."""
        s = self._store
        if self._portfolio is not None:
            s.save_portfolio(self._portfolio)  # 1: books first, so the log is never ahead of them
        if self._log:
            s.append_trades_log(self._log)  # 2
        for pid, doc in self._proposals.items():  # 3
            s.save_proposal(pid, doc)
        if self._nav_rows:  # 4
            s.save_nav_history(self._nav_rows)
        s.save_extras(evaluations=self._evals, acks=self._acks)
        if self._run_note is not None:  # 5
            s.save_extras(run_notes={self._run_note.name: self._run_note.text})
        s.save_extras(dashboard=self._dashboard)  # 6
        s.save_extras(alerts=self._alerts)  # 7
