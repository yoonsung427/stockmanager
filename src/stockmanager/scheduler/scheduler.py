"""Due-job determination (AS §1.5): a pure function of the books, config and scheduler state (⚑AD4).

There are no per-proposal OS jobs; ``list_due_jobs``/``list_all_jobs`` derive the whole schedule on every call, so
sleep/wake and retry-within-expiry are properties of this code, not of cron. Execution jobs sort before evaluation
(AS §1.5's tick algorithm).

``RECONFIRM`` creates its replacement proposal at once but validates it later (AS §8.7): a ``PROPOSED`` proposal
with ``reconfirm_check == "pending"`` has a ``reconfirm:<id>`` job, due on the next tick.

Deviation from the architecture spec: this build's ``ProposalDoc`` has no ``delivery`` field (AS §8.3), so there is no
delivery gate other than ``reconfirm_check``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from stockmanager.adapters.yahoo import SETTLE
from stockmanager.config.model import Config
from stockmanager.domain.docs import ProposalDoc, Status
from stockmanager.jobs.common import NY, TradingCalendar
from stockmanager.scheduler.slots import latest_slot
from stockmanager.scheduler.state import SchedulerState
from stockmanager.triage.wakes import TriageWake

DEFAULT_EXEC_DELAY_MINUTES = 5
DEFAULT_LATE_THRESHOLD_MINUTES = 10
#: The price check runs 15 minutes after the open (09:45 ET on a regular day).
PRICE_CHECK_DELAY = timedelta(minutes=15)
#: The digest runs once the close has settled (``SETTLE`` after 16:00 ET): Yahoo will not serve the close before that.
#: It is retried every tick until it succeeds, but only until this long after it first became due.
DIGEST_DELAY = SETTLE
DIGEST_RETRY_WINDOW = timedelta(hours=3)
#: At most this many ``triage_escalation`` jobs per tick (v2 R7): each is a full off-cycle run, so a burst of wakes
#: is drained across ticks rather than monopolising one.
MAX_TRIAGE_JOBS_PER_TICK = 1


@dataclass(frozen=True)
class ScheduleSlot:
    """One evaluation slot: its PT instant, cycle id, and whether it is running late (AS §1.5)."""

    slot_time: datetime
    cycle_id: str
    late: bool


@dataclass(frozen=True)
class DueJob:
    """One derived job (AS §1.5). ``proposal_id`` is set for ``execution`` and ``reconfirm``.

    ``triage_escalation`` (v2 D1/R7) carries a ``ScheduleSlot`` whose ``cycle_id`` is the wake's ``triage-...`` id and
    whose ``slot_time`` is the wake's ``due_at``. Its cycle id is not a date, so it never collides with the weekly slot.
    """

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


def _by_id(p: ProposalDoc) -> str:
    return p.proposal_id


def _is_pending_reconfirm(p: ProposalDoc) -> bool:
    return p.state == "PROPOSED" and p.reconfirm_check == "pending"


def list_due_jobs(
    now: datetime,
    cfg: Config,
    status: Status,
    proposals: list[ProposalDoc],
    scheduler_state: SchedulerState,
    *,
    exec_delay_minutes: int = DEFAULT_EXEC_DELAY_MINUTES,
    late_threshold_minutes: int = DEFAULT_LATE_THRESHOLD_MINUTES,
    calendar: TradingCalendar | None = None,
) -> list[DueJob]:
    """Jobs due *now* (AS §1.5 table). Execution jobs first, then evaluation, matching the tick's dispatch order.

    With a ``calendar`` and ``cfg.summary.enabled`` a ``LIVE`` portfolio also gets the ``price_check`` and
    ``digest`` jobs (see ``_summary_jobs``); without one, no summary job is ever due.
    """
    jobs: list[DueJob] = []
    if status == "LIVE":
        jobs.extend(_execution_jobs(now, proposals, exec_delay_minutes))
        jobs.extend(DueJob("reconfirm", None, p.proposal_id) for p in sorted(proposals, key=_by_id) if _is_pending_reconfirm(p))
    if status in ("LIVE", "PAUSED"):
        slot = _eval_slot(now, cfg, scheduler_state, late_threshold_minutes)
        if now >= slot.slot_time and slot.slot_time.date() not in scheduler_state.completed_slots:
            jobs.append(DueJob("evaluation", slot, None))
    if status == "APPROVED":
        jobs.append(DueJob("opening", None, None))
    if status == "LIVE" and calendar is not None:
        jobs.extend(_summary_jobs(now, cfg, scheduler_state, calendar))
    if status == "LIVE":
        jobs.extend(_triage_jobs(now, scheduler_state))  # after the weekly slot: an off-cycle wake never pre-empts it
    return jobs


def _triage_wake_slot(wake: TriageWake) -> ScheduleSlot:
    return ScheduleSlot(slot_time=wake.due_at, cycle_id=wake.cycle_id, late=False)


def _triage_jobs(now: datetime, scheduler_state: SchedulerState) -> list[DueJob]:
    """Due, not-yet-dispatched triage wakes, oldest first, capped at ``MAX_TRIAGE_JOBS_PER_TICK``.

    A wake deferred through quiet hours has ``due_at`` at the morning delivery, so it is not due before then.
    """
    done = set(scheduler_state.triage_done)
    due = sorted(
        (w for w in scheduler_state.triage_wakes if w.cycle_id not in done and w.due_at <= now),
        key=lambda w: (w.due_at, w.cycle_id),
    )
    return [DueJob("triage_escalation", _triage_wake_slot(w), None) for w in due[:MAX_TRIAGE_JOBS_PER_TICK]]


def _summary_jobs(now: datetime, cfg: Config, scheduler_state: SchedulerState, calendar: TradingCalendar) -> list[DueJob]:
    """``price_check`` (open + 15 min, until the close) and ``digest`` (close + settle, until the retry window ends).

    Both are keyed by the New York date, run on trading days only, and run once per day (``scheduler_state``). The
    digest also rolls ``last_close.json`` forward, so it is due whenever either summary feature is on.
    """
    if not cfg.summary.enabled:
        return []
    day = now.astimezone(NY).date()
    try:
        if not calendar.is_session(day):
            return []
        opens, closes = calendar.session_open(day), calendar.session_close(day)
    except ValueError:  # outside the calendar's coverage: fail closed, like every other calendar user
        return []
    jobs: list[DueJob] = []
    if (
        cfg.summary.alert_on_threshold
        and day not in scheduler_state.price_check_days
        and opens + PRICE_CHECK_DELAY <= now < closes
    ):
        jobs.append(DueJob("price_check", None))
    due_at = closes + DIGEST_DELAY
    if (
        (cfg.summary.digest_at_close or cfg.summary.alert_on_threshold)
        and day not in scheduler_state.digest_days
        and due_at <= now < due_at + DIGEST_RETRY_WINDOW
    ):
        jobs.append(DueJob("digest", None))
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
        for p in sorted(proposals, key=_by_id):
            if _is_pending_reconfirm(p):
                jobs.append(DueJob("reconfirm", None, p.proposal_id))
    if status in ("LIVE", "PAUSED"):
        jobs.append(DueJob("evaluation", _eval_slot(now, cfg, scheduler_state, late_threshold_minutes), None))
    if status == "APPROVED":
        jobs.append(DueJob("opening", None, None))
    if status == "LIVE":
        done = set(scheduler_state.triage_done)
        jobs.extend(
            DueJob("triage_escalation", _triage_wake_slot(w), None)
            for w in scheduler_state.triage_wakes
            if w.cycle_id not in done
        )
    return jobs
