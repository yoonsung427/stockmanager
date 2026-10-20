"""Tick dispatcher (AS §1.5): single-flight, config reload, outbox drain, sweep, derived job dispatch, housekeeping."""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from stockmanager.config.model import Config
from stockmanager.config.reload import reload as reload_config
from stockmanager.domain.docs import ProposalDoc
from stockmanager.execution.engine import ExecutionEngine
from stockmanager.notify.deliver import Channel, deliver_pending
from stockmanager.notify.templates import use_catalogue
from stockmanager.scheduler import state as scheduler_state_mod
from stockmanager.scheduler.scheduler import list_due_jobs
from stockmanager.scheduler.sweeper import sweep
from stockmanager.store import FileStore
from stockmanager.store.lock import Clock

if TYPE_CHECKING:
    from stockmanager.evaluation.engine import EvaluationEngine
    from stockmanager.evaluation.reconfirm import ReconfirmEngine

TICK_LOCK_NAME = "tick.lock"
OUTBOX_BUDGET_S = 20.0  # AS §1.5: notify.deliver_pending(budget=20 s)
MAX_RUNTIME_S = 20 * 60.0  # AS §1.5: watchdog max_runtime 20 min


class TickTimeout(Exception):
    """The tick exceeded ``max_runtime``; raised by ``Watchdog.check`` at a stage boundary (AS §1.5, §10.1)."""


class Watchdog:
    """Cooperative tick watchdog (AS §1.5): raises ``TickTimeout`` at stage boundaries, never mid-commit.

    A signal or helper thread that interrupts the tick would fire inside a ``Transaction`` commit and tear the
    books, which AS §10.1 forbids. So the deadline is only ever *checked*, between stages, by ``check``. A stage
    that itself hangs is bounded by its own timeouts (HTTP, ``portfolio.lock`` retries), not by the watchdog.
    """

    def __init__(self, max_runtime: float = MAX_RUNTIME_S, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._max_runtime = max_runtime
        self._started = monotonic()

    def check(self, stage: str) -> None:
        """Raise ``TickTimeout`` if the tick has run longer than ``max_runtime``; call only between stages."""
        elapsed = self._monotonic() - self._started
        if elapsed > self._max_runtime:
            raise TickTimeout(f"tick exceeded {self._max_runtime:.0f}s before stage {stage!r} ({elapsed:.0f}s)")


def _load_all_proposals(store: FileStore) -> list[ProposalDoc]:
    directory = store.root / "proposals"
    if not directory.exists():
        return []
    out: list[ProposalDoc] = []
    for path in sorted(directory.glob("*.json")):
        try:
            out.append(ProposalDoc.model_validate_json(path.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return out


@contextmanager
def tick_lock(root: Path) -> Iterator[bool]:
    """Hold ``state/tick.lock`` non-blocking for the ``with`` body; yields ``False`` if another tick holds it.

    Callers take this *before* building anything expensive (AS §1.5: single-flight, then work).
    """
    lock_path = root / "state" / TICK_LOCK_NAME
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
        os.close(fd)  # closing the descriptor drops the flock


def tick(
    clock: Clock,
    store: FileStore,
    cfg: Config,
    *,
    execution: ExecutionEngine | None = None,
    evaluation: EvaluationEngine | None = None,
    reconfirm: ReconfirmEngine | None = None,
    channels: Sequence[Channel] = (),
    max_runtime: float = MAX_RUNTIME_S,
) -> int:
    """Run one tick (AS §1.5). Single-flight on ``state/tick.lock``; returns 0 (handled) or 1 (unhandled error).

    ``evaluation``, when given, actually runs the due evaluation slot (AS §1.2); the slot is only marked complete
    on ``RunResult(\"completed\")`` so a busy lock, a reconcile halt or a price failure retries on the next tick
    rather than being silently skipped. When ``evaluation`` is omitted (for callers that only exercise the
    dispatch plumbing) the slot is marked complete unconditionally, as before.
    """
    with tick_lock(store.root) as acquired:
        if not acquired:
            return 0  # another tick holds state/tick.lock: skip silently (AS §1.5)
        return run_locked(
            clock,
            store,
            cfg,
            execution=execution,
            evaluation=evaluation,
            reconfirm=reconfirm,
            channels=channels,
            max_runtime=max_runtime,
        )


def run_locked(
    clock: Clock,
    store: FileStore,
    cfg: Config,
    *,
    execution: ExecutionEngine | None = None,
    evaluation: EvaluationEngine | None = None,
    reconfirm: ReconfirmEngine | None = None,
    channels: Sequence[Channel] = (),
    max_runtime: float = MAX_RUNTIME_S,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """The tick body, for a caller that already holds ``tick_lock``. Never raises; returns 0 or 1 (AS §10)."""
    try:
        return _run(clock, store, cfg, execution, evaluation, reconfirm, channels, Watchdog(max_runtime, monotonic))
    except Exception:  # noqa: BLE001 - top-level catch (incl. TickTimeout): the tick always exits cleanly (AS §10)
        return 1


def _run(
    clock: Clock,
    store: FileStore,
    cfg: Config,
    execution: ExecutionEngine | None,
    evaluation: EvaluationEngine | None,
    reconfirm: ReconfirmEngine | None,
    channels: Sequence[Channel],
    watchdog: Watchdog,
) -> int:
    now = clock()
    # Heartbeat first: a long evaluation must not look like a dead scheduler to the bot's stall check (AS §10.1).
    scheduler_state = scheduler_state_mod.load(store.root).with_last_tick(now)
    scheduler_state_mod.save(store.root, scheduler_state)

    watchdog.check("config_reload")
    cfg = reload_config(store, clock, fallback=cfg)
    use_catalogue(cfg.ops.notification_catalogue)  # config.yaml may have changed the alert text since the last tick
    if channels:
        watchdog.check("outbox")
        deliver_pending(
            store, channels, clock, time_budget=OUTBOX_BUDGET_S, visible_channels=cfg.ops.visible_channels
        )
    watchdog.check("sweep")
    sweep(now, store, clock, cfg.ops.visible_channels)

    if not store.portfolio_exists():
        return 0
    portfolio = store.load_portfolio()
    proposals = _load_all_proposals(store)

    try:
        if portfolio.status in ("LIVE", "PAUSED"):
            for job in list_due_jobs(now, cfg, portfolio.status, proposals, scheduler_state):
                watchdog.check(f"job:{job.kind}")
                if job.kind == "execution" and job.proposal_id is not None and portfolio.status == "LIVE":
                    if execution is not None:
                        execution.run_proposal(job.proposal_id)
                elif job.kind == "reconfirm" and job.proposal_id is not None and portfolio.status == "LIVE":
                    if reconfirm is not None:
                        reconfirm.run(job.proposal_id, cfg)  # not-passed outcomes stay pending/ended; see its docs
                elif job.kind == "evaluation" and job.slot is not None:
                    if portfolio.status == "LIVE":
                        if evaluation is None:
                            scheduler_state = scheduler_state.with_completed(job.slot.slot_time.date())
                        else:
                            result = evaluation.run(job.slot, cfg)
                            if result.outcome == "completed":
                                scheduler_state = scheduler_state.with_completed(job.slot.slot_time.date())
                            # any other outcome (lock_busy, halted_reconcile, failed_closed, ...) leaves the slot
                            # due so the next tick retries it, matching AS §1.5's "derive, never schedule" design.
                    else:  # PAUSED: one "paused" notice per slot, never run the evaluation itself
                        scheduler_state = scheduler_state.with_noticed(job.slot.slot_time.date())
        elif portfolio.status == "APPROVED":
            for job in list_due_jobs(now, cfg, portfolio.status, proposals, scheduler_state):
                if job.kind == "opening" and execution is not None:
                    watchdog.check("job:opening")
                    execution.run_opening()
        # STAGED / CLOSED: nothing to dispatch (AS §1.5)
    finally:
        # Persist slots completed so far even if the watchdog fired before the next job: a finished evaluation
        # must not be re-run just because a later stage ran out of time.
        scheduler_state_mod.save(store.root, scheduler_state)
    return 0
