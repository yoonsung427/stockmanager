"""Tick dispatcher (AS §1.5): single-flight, config reload, outbox drain, sweep, derived job dispatch, housekeeping."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Sequence
from typing import TYPE_CHECKING

from stockmanager.config.model import Config
from stockmanager.config.reload import reload as reload_config
from stockmanager.domain.docs import ProposalDoc
from stockmanager.execution.engine import ExecutionEngine
from stockmanager.notify.deliver import Channel, deliver_pending
from stockmanager.scheduler import state as scheduler_state_mod
from stockmanager.scheduler.scheduler import list_due_jobs
from stockmanager.scheduler.sweeper import sweep
from stockmanager.store import FileStore
from stockmanager.store.lock import Clock

if TYPE_CHECKING:
    from stockmanager.evaluation.engine import EvaluationEngine

TICK_LOCK_NAME = "tick.lock"
OUTBOX_BUDGET = 20


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


def tick(
    clock: Clock,
    store: FileStore,
    cfg: Config,
    *,
    execution: ExecutionEngine | None = None,
    evaluation: EvaluationEngine | None = None,
    channels: Sequence[Channel] = (),
) -> int:
    """Run one tick (AS §1.5). Single-flight on ``state/tick.lock``; returns 0 (handled) or 1 (unhandled error).

    ``evaluation``, when given, actually runs the due evaluation slot (AS §1.2); the slot is only marked complete
    on ``RunResult(\"completed\")`` so a busy lock, a reconcile halt or a price failure retries on the next tick
    rather than being silently skipped. When ``evaluation`` is omitted (for callers that only exercise the
    dispatch plumbing) the slot is marked complete unconditionally, as before.
    """
    lock_path = store.root / "state" / TICK_LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # another tick holds state/tick.lock: skip silently (AS §1.5)
        try:
            return _run(clock, store, cfg, execution, evaluation, channels)
        except Exception:  # noqa: BLE001 - top-level catch: the tick always exits cleanly (AS §10)
            return 1
    finally:
        os.close(fd)


def _run(
    clock: Clock,
    store: FileStore,
    cfg: Config,
    execution: ExecutionEngine | None,
    evaluation: EvaluationEngine | None,
    channels: Sequence[Channel],
) -> int:
    now = clock()
    cfg = reload_config(store, clock, fallback=cfg)
    if channels:
        deliver_pending(store, channels, clock, budget=OUTBOX_BUDGET)
    sweep(now, store, clock)

    scheduler_state = scheduler_state_mod.load(store.root)
    if not store.portfolio_exists():
        scheduler_state_mod.save(store.root, scheduler_state.with_last_tick(now))
        return 0
    portfolio = store.load_portfolio()
    proposals = _load_all_proposals(store)

    if portfolio.status in ("LIVE", "PAUSED"):
        for job in list_due_jobs(now, cfg, portfolio.status, proposals, scheduler_state):
            if job.kind == "execution" and job.proposal_id is not None and portfolio.status == "LIVE":
                if execution is not None:
                    execution.run_proposal(job.proposal_id)
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
                execution.run_opening()
    # STAGED / CLOSED: nothing to dispatch (AS §1.5)

    scheduler_state_mod.save(store.root, scheduler_state.with_last_tick(now))
    return 0
