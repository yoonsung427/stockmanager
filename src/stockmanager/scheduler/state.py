"""``state/scheduler.json`` (AS §3.10): scheduler bookkeeping only, not the books; always rebuildable.

Written with no lock (AS ownership matrix §3.11): a crash leaves, at worst, a stale completed-slots list that the
next evaluation run simply treats as not-yet-complete, so it re-derives (P7).
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from stockmanager.store.fsutil import atomic_write_text
from stockmanager.triage.wakes import TriageWake

MAX_COMPLETED_SLOTS = 8
MAX_TRIAGE_DONE = 64


@dataclass(frozen=True)
class SchedulerState:
    """``{anchor_date, completed_slots[<=8], last_tick, noticed_slots[], digest_days[<=8], price_check_days[<=8]}``.

    ``digest_days``/``price_check_days`` are additions to AS §3.10: the ET session dates on which the summary jobs
    have already run.

    ``triage_done`` (v2 R7) holds the cycle ids (``triage-...``, never a date) of triage wakes already dispatched;
    it is separate from ``completed_slots`` so an off-cycle run can never mark the weekly slot complete.
    ``triage_wakes`` is NOT persisted: the tick rebuilds it from ``triage-wakes/`` each run, because the triage cron
    job (another process) writes wakes and ``scheduler.json`` is written lock-free by the tick alone.
    """

    anchor_date: date | None = None
    completed_slots: tuple[date, ...] = field(default_factory=tuple)
    last_tick: datetime | None = None
    noticed_slots: tuple[date, ...] = field(default_factory=tuple)
    digest_days: tuple[date, ...] = field(default_factory=tuple)
    price_check_days: tuple[date, ...] = field(default_factory=tuple)
    triage_done: tuple[str, ...] = field(default_factory=tuple)
    triage_wakes: tuple[TriageWake, ...] = field(default_factory=tuple)

    def to_json(self) -> str:
        """Canonical JSON for ``state/scheduler.json``."""
        payload = {
            "anchor_date": self.anchor_date.isoformat() if self.anchor_date else None,
            "completed_slots": [d.isoformat() for d in self.completed_slots],
            "last_tick": self.last_tick.isoformat() if self.last_tick else None,
            "noticed_slots": [d.isoformat() for d in self.noticed_slots],
            "digest_days": [d.isoformat() for d in self.digest_days],
            "price_check_days": [d.isoformat() for d in self.price_check_days],
            "triage_done": list(self.triage_done),
        }
        return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> SchedulerState:
        """Inverse of ``to_json``."""
        data = json.loads(text)
        return cls(
            anchor_date=date.fromisoformat(data["anchor_date"]) if data.get("anchor_date") else None,
            completed_slots=tuple(date.fromisoformat(d) for d in data.get("completed_slots", [])),
            last_tick=datetime.fromisoformat(data["last_tick"]) if data.get("last_tick") else None,
            noticed_slots=tuple(date.fromisoformat(d) for d in data.get("noticed_slots", [])),
            digest_days=tuple(date.fromisoformat(d) for d in data.get("digest_days", [])),
            price_check_days=tuple(date.fromisoformat(d) for d in data.get("price_check_days", [])),
            triage_done=tuple(str(c) for c in data.get("triage_done", [])),
        )

    def with_completed(self, slot: date) -> SchedulerState:
        """New state with ``slot`` added to ``completed_slots`` (deduped, capped, anchor set on the first one)."""
        slots = tuple(sorted({*self.completed_slots, slot}))[-MAX_COMPLETED_SLOTS:]
        return dataclasses.replace(self, completed_slots=slots, anchor_date=self.anchor_date or slot)

    def with_digest_done(self, day: date) -> SchedulerState:
        """New state with the daily digest for ``day`` (an ET session date) marked done; capped like ``completed_slots``."""
        days = tuple(sorted({*self.digest_days, day}))[-MAX_COMPLETED_SLOTS:]
        return dataclasses.replace(self, digest_days=days)

    def with_price_check_done(self, day: date) -> SchedulerState:
        """New state with the morning price check for ``day`` (an ET session date) marked done."""
        days = tuple(sorted({*self.price_check_days, day}))[-MAX_COMPLETED_SLOTS:]
        return dataclasses.replace(self, price_check_days=days)

    def with_triage_wakes(self, wakes: tuple[TriageWake, ...]) -> SchedulerState:
        """New state carrying the pending wake files read from ``triage-wakes/`` (transient, not persisted)."""
        return dataclasses.replace(self, triage_wakes=wakes)

    def with_triage_done(self, cycle_id: str) -> SchedulerState:
        """New state with the triage wake ``cycle_id`` marked dispatched (deduped, capped to the newest)."""
        done = tuple(dict.fromkeys([*self.triage_done, cycle_id]))[-MAX_TRIAGE_DONE:]
        return dataclasses.replace(self, triage_done=done)

    def with_noticed(self, slot: date) -> SchedulerState:
        """New state with ``slot`` added to ``noticed_slots`` (deduped)."""
        return dataclasses.replace(self, noticed_slots=tuple(sorted({*self.noticed_slots, slot})))

    def with_last_tick(self, now: datetime) -> SchedulerState:
        """New state with ``last_tick`` set to ``now``."""
        return dataclasses.replace(self, last_tick=now)


def path(root: Path) -> Path:
    """``<root>/state/scheduler.json``."""
    return root / "state" / "scheduler.json"


def load(root: Path) -> SchedulerState:
    """Read ``state/scheduler.json``; an absent or unparsable file is a fresh, empty state."""
    p = path(root)
    if not p.exists():
        return SchedulerState()
    try:
        return SchedulerState.from_json(p.read_text(encoding="utf-8"))
    except (ValueError, KeyError, TypeError):
        return SchedulerState()


def save(root: Path, state: SchedulerState) -> None:
    """Atomically write ``state/scheduler.json``."""
    atomic_write_text(path(root), state.to_json())
