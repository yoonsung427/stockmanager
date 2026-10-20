"""Scheduler (AS §1.5): slot arithmetic, derived jobs, the proposal sweeper and the tick dispatcher."""

from __future__ import annotations

from stockmanager.scheduler.scheduler import DueJob, ScheduleSlot, list_all_jobs, list_due_jobs
from stockmanager.scheduler.slots import completed_slots, latest_slot, next_slot
from stockmanager.scheduler.state import SchedulerState
from stockmanager.scheduler.sweeper import sweep
from stockmanager.scheduler.tick import tick

__all__ = [
    "DueJob",
    "ScheduleSlot",
    "SchedulerState",
    "completed_slots",
    "latest_slot",
    "list_all_jobs",
    "list_due_jobs",
    "next_slot",
    "sweep",
    "tick",
]
