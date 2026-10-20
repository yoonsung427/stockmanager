"""Delivery (AS §9.2): the serialised loop that sends pending alerts and moves them to ``sent``/``dead``.

Delivery is at-least-once: a crash between a successful ``channel.send`` and recording the receipt can resend an
alert, which a channel's ``nonce`` enforcement should suppress. Order is preserved per channel: a ``Transient``
failure stops that channel's queue so a later alert can never overtake an earlier one (TS EDGE-10).
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from stockmanager.domain.docs import DeliveryReceipt, ProposalDoc
from stockmanager.domain.proposal_state import is_terminal
from stockmanager.domain.timeutil import add_elapsed
from stockmanager.notify.outbox import Alert, enqueue
from stockmanager.store import FileStore
from stockmanager.store import fsutil as _fsutil
from stockmanager.store.fsutil import atomic_write_text
from stockmanager.store.lock import Clock, LockBusy

SEVERITY_COLORS: dict[str, int] = {"positive": 0x2ECC71, "alert": 0xE74C3C, "info": 0x3498DB}

BACKOFF_FLOOR_S = 5.0
BACKOFF_CAP_S = 300.0
MAX_PERMANENT_ATTEMPTS = 3
TITLE_MAX_CHARS = 256
DESCRIPTION_MAX_CHARS = 4096
MAX_EMBED_FIELDS = 25

#: Channels whose receipt starts a veto window when ``ops.visible_channels`` is not supplied (AS §8.3, ⚑AD12).
DEFAULT_VISIBLE_CHANNELS: tuple[str, ...] = ("discord",)


class Transient(Exception):
    """A temporary delivery failure (network error, 5xx, 429); honours ``retry_after`` where the channel reports it."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Permanent(Exception):
    """A delivery failure that retrying will not fix (401/403/404/400); dead-lettered after ``MAX_PERMANENT_ATTEMPTS``."""


@dataclass(frozen=True)
class Rendered:
    """What ``render`` produces for one channel: plain text, plus a Discord embed where applicable."""

    text: str
    embed: dict[str, Any] | None = None


@dataclass(frozen=True)
class Receipt:
    """Acknowledgement of a successful send."""

    channel: str
    external_id: str | None
    sent_at: datetime


class Channel(Protocol):
    """A delivery target (AS §9.2)."""

    name: Literal["discord", "mainchat"]

    def send(self, rendered: Rendered, *, nonce: str) -> Receipt:
        """Send ``rendered``, deduped by ``nonce``. Raises ``Transient`` or ``Permanent`` on failure."""
        ...


def render(alert: Alert, channel: str, now: datetime, *, deadline: datetime | None = None) -> Rendered:
    """Render ``alert`` for ``channel`` as of ``now``: late-bound fields (veto deadline) are filled in here.

    ``deadline`` pins the veto deadline (a channel that succeeds after another reuses the first receipt's, AS §8.3);
    by default it is ``now`` plus the alert's ``window_hours``.
    """
    body = alert.body
    if "window_hours" in alert.binds:
        if deadline is None:
            deadline = add_elapsed(now, timedelta(hours=int(alert.binds["window_hours"])))
        body = body.format(deadline=deadline.isoformat())
    commands_block = "\n".join(alert.commands)
    if channel == "discord":
        fields = [{"name": k, "value": v, "inline": True} for k, v in alert.fields.items()]
        if alert.commands:
            fields.append({"name": "Commands", "value": f"```\n{commands_block}\n```", "inline": False})
        embed = {
            "title": alert.title[:TITLE_MAX_CHARS],
            "description": body[:DESCRIPTION_MAX_CHARS],
            "color": SEVERITY_COLORS[alert.severity],
            "fields": fields[:MAX_EMBED_FIELDS],
            "timestamp": (alert.occurred_at or now).isoformat(),
        }
        return Rendered(text=alert.title, embed=embed)
    lines = [f"**{alert.title}**", "", body]
    if alert.fields:
        lines += ["", *(f"- **{k}**: {v}" for k, v in alert.fields.items())]
    if alert.commands:
        lines += ["", "```", commands_block, "```"]
    return Rendered(text="\n".join(lines), embed=None)


def _backoff_seconds(attempts: int) -> float:
    """5 s, 10 s, 20 s, ... capped at 5 min (AS §9.2)."""
    return float(min(BACKOFF_FLOOR_S * (2 ** max(attempts - 1, 0)), BACKOFF_CAP_S))


def deliver_pending(
    store: FileStore,
    channels: Sequence[Channel],
    clock: Clock,
    budget: int = 100,
    *,
    time_budget: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    visible_channels: Sequence[str] = DEFAULT_VISIBLE_CHANNELS,
) -> list[str]:
    """Deliver up to ``budget`` pending alerts, oldest first, serialised by ``alerts/.deliver.lock``.

    ``time_budget`` (seconds, AS §1.5's ``budget=20 s``) additionally bounds the drain: once it has elapsed no
    further send is started, so the rest of the queue waits for the next call. A send already in flight is not
    interrupted (the channel's own HTTP timeout bounds it). Progress made before the cutoff is still recorded.
    The clock for the budget is ``monotonic`` so wall-clock steps cannot extend or cut it short.

    A successful send of a ``veto_window_open`` alert to one of ``visible_channels`` appends a receipt to the
    proposal (AS §8.3); the sweeper opens the veto window from it.

    Returns the ``alert_id`` of every (alert, channel) send that succeeded this call.
    """
    pending_dir = store.root / "alerts" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    guard_path = pending_dir.parent / ".deliver.lock"
    fd = os.open(guard_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        out_of_time = _deadline(time_budget, monotonic)
        return _deliver_locked(store, channels, clock, budget, pending_dir, out_of_time, frozenset(visible_channels))
    finally:
        os.close(fd)


def _deadline(time_budget: float | None, monotonic: Callable[[], float]) -> Callable[[], bool]:
    """A predicate that turns true once ``time_budget`` seconds have passed since this call (never, if ``None``).

    Started after ``.deliver.lock`` is held, so time spent waiting on another drainer is not charged to the budget.
    """
    if time_budget is None:
        return lambda: False
    deadline = monotonic() + time_budget
    return lambda: monotonic() >= deadline


def _deliver_locked(
    store: FileStore,
    channels: Sequence[Channel],
    clock: Clock,
    budget: int,
    pending_dir: Path,
    out_of_time: Callable[[], bool],
    visible: frozenset[str],
) -> list[str]:
    paths = sorted(pending_dir.glob("*.json"))[:budget]
    loaded = [(path, Alert.from_json(path.read_text(encoding="utf-8"))) for path in paths]
    now = clock()
    delivered: list[str] = []
    touched: set[str] = set()
    dead: set[str] = set()
    for channel in channels:
        if out_of_time():
            break
        for path, alert in loaded:
            if out_of_time():
                break
            if alert.alert_id in dead:
                continue
            state = alert.channels.get(channel.name)
            if state is None or state.state == "sent":
                continue
            if state.next_attempt_at is not None and state.next_attempt_at > now:
                continue
            proposal_id = _veto_proposal_id(alert)
            deadline = _fixed_deadline(store, proposal_id, alert, now) if proposal_id is not None else None
            rendered = render(alert, channel.name, now, deadline=deadline)
            try:
                receipt = channel.send(rendered, nonce=alert.alert_id)
                if proposal_id is not None and deadline is not None and channel.name in visible:
                    # Before the alert is marked sent: if the receipt cannot be recorded the send is retried
                    # (at-least-once; the channel's nonce suppresses the duplicate) rather than lost.
                    _record_receipt(store, proposal_id, channel.name, now, deadline)
            except (Transient, LockBusy) as exc:
                retry_after = exc.retry_after if isinstance(exc, Transient) else None
                state.attempts += 1
                wait = retry_after if retry_after is not None else _backoff_seconds(state.attempts)
                state.next_attempt_at = now + timedelta(seconds=wait)
                state.last_error = str(exc)
                touched.add(alert.alert_id)
                break  # preserve order: this channel's queue stops here, never overtaken by a later alert
            except Permanent as exc:
                state.attempts += 1
                state.last_error = str(exc)
                touched.add(alert.alert_id)
                if state.attempts >= MAX_PERMANENT_ATTEMPTS:
                    state.state = "dead"
                    _dead_letter(store, path, alert, channel.name)
                    dead.add(alert.alert_id)
                continue
            else:
                state.state = "sent"
                state.receipt = receipt.external_id
                touched.add(alert.alert_id)
                delivered.append(alert.alert_id)
    for path, alert in loaded:
        if alert.alert_id in dead:
            continue  # already moved to dead/, its pending file is gone
        if alert.channels and all(s.state == "sent" for s in alert.channels.values()):
            _mark_sent(store, path, alert)
        elif alert.alert_id in touched:
            atomic_write_text(path, alert.to_json())
    return delivered


def _veto_proposal_id(alert: Alert) -> str | None:
    """The proposal a ``veto_window_open`` alert opens the window for, or ``None`` for any other alert."""
    proposal_id = alert.binds.get("proposal_id")
    if alert.kind != "veto_window_open" or not isinstance(proposal_id, str) or "window_hours" not in alert.binds:
        return None
    return proposal_id


def _load_proposal(store: FileStore, proposal_id: str) -> ProposalDoc | None:
    try:
        return ProposalDoc.model_validate_json(store.proposal_path(proposal_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # missing, unsafe id or quarantined file: nothing to record a receipt on


def _fixed_deadline(store: FileStore, proposal_id: str, alert: Alert, now: datetime) -> datetime:
    """The veto deadline to render: the first receipt's if one exists, else ``now + window_hours`` (AS §8.3)."""
    doc = _load_proposal(store, proposal_id)
    if doc is not None and doc.delivery.receipts:
        return doc.delivery.receipts[0].veto_deadline
    return add_elapsed(now, timedelta(hours=int(alert.binds["window_hours"])))


def _record_receipt(store: FileStore, proposal_id: str, channel: str, now: datetime, deadline: datetime) -> None:
    """Append ``{channel, delivered_at, veto_deadline}`` to the proposal's ``delivery.receipts`` under the lock.

    The lock matters: the sweeper rewrites the same file in its own transaction, and an unlocked read-modify-write
    here could erase one of its transitions. Raises ``LockBusy`` if the lock cannot be taken. A terminal proposal, a
    missing file or a channel that already has a receipt is left untouched.
    """
    with store.transaction("deliver", max_attempts=1, sleep_seconds=0.0) as txn:
        doc = _load_proposal(store, proposal_id)
        if doc is None or is_terminal(doc.state) or any(r.channel == channel for r in doc.delivery.receipts):
            return
        receipt = DeliveryReceipt(channel=channel, delivered_at=now, veto_deadline=deadline)
        updated = doc.delivery.model_copy(update={"receipts": [*doc.delivery.receipts, receipt]})
        txn.stage_proposals({proposal_id: doc.model_copy(update={"delivery": updated})})


def _dead_letter(store: FileStore, path: Path, alert: Alert, channel_name: str) -> None:
    """Move ``alert`` to ``alerts/dead/`` and enqueue a main-chat notice about the misconfiguration."""
    dead_dir = store.root / "alerts" / "dead"
    atomic_write_text(dead_dir / path.name, alert.to_json())
    path.unlink(missing_ok=True)
    from stockmanager.notify.outbox import ChannelState  # local import: avoids a module-level cycle with outbox

    notice = Alert(
        kind="delivery_misconfigured",
        severity="alert",
        title=f"{channel_name.capitalize()} delivery misconfigured",
        body=(
            f"Alert {alert.alert_id} ({alert.kind}) could not be delivered to {channel_name} after "
            f"{MAX_PERMANENT_ATTEMPTS} attempts and was moved to the dead letter queue."
        ),
        fields={"channel": channel_name, "original_kind": alert.kind, "original_alert_id": alert.alert_id},
        commands=["STATUS"],
        needs_action=True,
        dedupe_key=f"delivery_misconfigured:{channel_name}",
        binds={},
        channels={"mainchat": ChannelState()},
    )
    enqueue(notice, store)


def _mark_sent(store: FileStore, path: Path, alert: Alert) -> None:
    """Persist the final per-channel state, then atomically move the file from ``pending/`` to ``sent/``."""
    sent_dir = store.root / "alerts" / "sent"
    sent_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, alert.to_json())
    target = sent_dir / path.name
    os.replace(path, target)
    _fsutil.fsync_dir(sent_dir)
