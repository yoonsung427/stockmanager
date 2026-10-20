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

from stockmanager.domain.timeutil import add_elapsed
from stockmanager.notify.outbox import Alert, enqueue
from stockmanager.store import FileStore
from stockmanager.store import fsutil as _fsutil
from stockmanager.store.fsutil import atomic_write_text
from stockmanager.store.lock import Clock

SEVERITY_COLORS: dict[str, int] = {"positive": 0x2ECC71, "alert": 0xE74C3C, "info": 0x3498DB}

BACKOFF_FLOOR_S = 5.0
BACKOFF_CAP_S = 300.0
MAX_PERMANENT_ATTEMPTS = 3
TITLE_MAX_CHARS = 256
DESCRIPTION_MAX_CHARS = 4096
MAX_EMBED_FIELDS = 25


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


def render(alert: Alert, channel: str, now: datetime) -> Rendered:
    """Render ``alert`` for ``channel`` as of ``now``: late-bound fields (veto deadline) are filled in here."""
    body = alert.body
    if "window_hours" in alert.binds:
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
) -> list[str]:
    """Deliver up to ``budget`` pending alerts, oldest first, serialised by ``alerts/.deliver.lock``.

    ``time_budget`` (seconds, AS §1.5's ``budget=20 s``) additionally bounds the drain: once it has elapsed no
    further send is started, so the rest of the queue waits for the next call. A send already in flight is not
    interrupted (the channel's own HTTP timeout bounds it). Progress made before the cutoff is still recorded.
    The clock for the budget is ``monotonic`` so wall-clock steps cannot extend or cut it short.

    Returns the ``alert_id`` of every (alert, channel) send that succeeded this call.
    """
    pending_dir = store.root / "alerts" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    guard_path = pending_dir.parent / ".deliver.lock"
    fd = os.open(guard_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        out_of_time = _deadline(time_budget, monotonic)
        return _deliver_locked(store, channels, clock, budget, pending_dir, out_of_time)
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
            rendered = render(alert, channel.name, now)
            try:
                receipt = channel.send(rendered, nonce=alert.alert_id)
            except Transient as exc:
                state.attempts += 1
                wait = exc.retry_after if exc.retry_after is not None else _backoff_seconds(state.attempts)
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
