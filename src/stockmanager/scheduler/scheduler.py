"""Due-job determination (AS §1.5): a pure function of the books, config and scheduler state (⚑AD4).

There are no per-proposal OS jobs; ``list_due_jobs``/``list_all_jobs`` derive the whole schedule on every call, so
sleep/wake and retry-within-expiry are properties of this code, not of cron. Execution jobs sort before evaluation
(AS §1.5's tick algorithm).

Deviation from the architecture spec: this build's ``ProposalDoc`` has no ``delivery``/``reconfirm_check`` fields
(``RECONFIRM`` creates a fully-formed new proposal synchronously, AS §8.7), so no ``reconfirm:<id>`` job is derived
here -- there is nothing left for a tick to validate asynchronously.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from stockmanager.config.model import Config
from stockmanager.domain.docs import ProposalDoc, Status
from stockmanager.scheduler.slots import latest_slot
from stockmanager.scheduler.state import SchedulerState

DEFAULT_EXEC_DELAY_MINUTES = 5
DEFAULT_LATE_THRESHOLD_MINUTES = 10


@dataclass(frozen=True)
class ScheduleSlot:
    """One evaluation slot: its PT instant, cycle id, and whether it is running late (AS §1.5)."""

    slot_time: datetime
    cycle_id: str
    late: bool


@dataclass(frozen=True)
class DueJob:
    """One derived job (AS §1.5). ``proposal_id`` is set for ``execution``."""

    kind: str
    slot: ScheduleSlot | None
    proposal_id: str | None = None


def _eval_slot(now: datetime, cfg: Config, scheduler_state: SchedulerState, late_threshold_minutes: int) -> ScheduleSlot:
    slot_time = latest_slot(now, cfg, scheduler_state.anchor_date)
    return ScheduleSlot(
        slot_time=slot_time,
        cycle_id=slot_time.date().isoformat(),
        late=(now - slot_time) > timedelta(minutes=late_threshold_minutes),
    )


def _execution_jobs(now: datetime, proposals: list[ProposalDoc], exec_delay_minutes: int) -> list[DueJob]:
    jobs: list[DueJob] = []
    for p in sorted(proposals, key=lambda d: d.proposal_id):
        if p.state != "APPROVED":
            continue
        window_start = p.execute_at + timedelta(minutes=exec_delay_minutes)
        if window_start <= now <= p.expires_at:
            jobs.append(DueJob("execution", None, p.proposal_id))
    return jobs


def list_due_jobs(
    now: datetime,
    cfg: Config,
    status: Status,
    proposals: list[ProposalDoc],
    scheduler_state: SchedulerState,
    *,
    exec_delay_minutes: int = DEFAULT_EXEC_DELAY_MINUTES,
    late_threshold_minutes: int = DEFAULT_LATE_THRESHOLD_MINUTES,
) -> list[DueJob]:
    """Jobs due *now* (AS §1.5 table). Execution jobs first, then evaluation, matching the tick's dispatch order."""
    jobs: list[DueJob] = []
    if status == "LIVE":
        jobs.extend(_execution_jobs(now, proposals, exec_delay_minutes))
    if status in ("LIVE", "PAUSED"):
        slot = _eval_slot(now, cfg, scheduler_state, late_threshold_minutes)
        if now >= slot.slot_time and slot.slot_time.date() not in scheduler_state.completed_slots:
            jobs.append(DueJob("evaluation", slot, None))
    if status == "APPROVED":
        jobs.append(DueJob("opening", None, None))
    return jobs


def list_all_jobs(
    now: datetime,
    cfg: Config,
    status: Status,
    proposals: list[ProposalDoc],
    scheduler_state: SchedulerState,
    *,
    late_threshold_minutes: int = DEFAULT_LATE_THRESHOLD_MINUTES,
) -> list[DueJob]:
    """Every job that *exists*, whether or not it is due yet (for ``STATUS``, AS §1.5 "Exists when" column)."""
    jobs: list[DueJob] = []
    if status == "LIVE":
        for p in sorted(proposals, key=lambda d: d.proposal_id):
            if p.state == "APPROVED":
                jobs.append(DueJob("execution", None, p.proposal_id))
    if status in ("LIVE", "PAUSED"):
        jobs.append(DueJob("evaluation", _eval_slot(now, cfg, scheduler_state, late_threshold_minutes), None))
    if status == "APPROVED":
        jobs.append(DueJob("opening", None, None))
    return jobs
