"""The alert outbox (AS §9.2): ``alerts/pending`` is the durable queue; delivery moves entries to ``sent``/``dead``.

Alerts are plain JSON files, one per alert, named so that sorting by filename reproduces creation order. Nothing
here sends anything: ``enqueue`` only ever writes to ``pending/``, and ``notify.deliver`` owns the state machine
that moves files out of it.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from stockmanager.store import FileStore
from stockmanager.store.fsutil import atomic_write_text

Severity = Literal["info", "positive", "alert"]
DeliveryState = Literal["pending", "sent", "dead"]

#: Every alert is delivered on both channels unless a template says otherwise by pre-populating ``channels``.
DEFAULT_CHANNELS: tuple[str, ...] = ("discord", "mainchat")


#: How long an enqueued ``dedupe_key`` is remembered in ``state/dedupe_keys.json`` if its alert never leaves
#: ``pending/`` (backstop; delivery releases the key on sent/dead, which keeps "free again once delivered").
DEDUPE_RETENTION = timedelta(hours=24)


class NeedsActionError(ValueError):
    """A ``needs_action`` alert was built with no commands to act on (TS AC-04)."""


@dataclass
class ChannelState:
    """Per-channel delivery progress for one alert (AS §9.2)."""

    state: DeliveryState = "pending"
    attempts: int = 0
    next_attempt_at: datetime | None = None
    receipt: str | None = None
    last_error: str | None = None
    permanent_failures: int = 0  # ``Permanent`` raises so far; the TTL sweep dead-letters on any (v2 P0 ruling)


@dataclass
class Alert:
    """One notification (AS §9.2). ``alert_id`` and ``occurred_at`` are filled by ``enqueue`` if not already set."""

    kind: str
    severity: Severity
    title: str
    body: str
    fields: dict[str, str]
    commands: list[str]
    needs_action: bool
    dedupe_key: str | None
    binds: dict[str, Any]
    alert_id: str = ""
    occurred_at: datetime | None = None
    channels: dict[str, ChannelState] = field(default_factory=dict)

    def to_json(self) -> str:
        """Canonical JSON for one ``alerts/{pending,sent,dead}/<alert_id>.json`` file."""
        payload = {
            "alert_id": self.alert_id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "body": self.body,
            "fields": self.fields,
            "commands": self.commands,
            "needs_action": self.needs_action,
            "dedupe_key": self.dedupe_key,
            "binds": self.binds,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "channels": {
                name: {
                    "state": s.state,
                    "attempts": s.attempts,
                    "next_attempt_at": s.next_attempt_at.isoformat() if s.next_attempt_at else None,
                    "receipt": s.receipt,
                    "last_error": s.last_error,
                    "permanent_failures": s.permanent_failures,
                }
                for name, s in self.channels.items()
            },
        }
        return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Alert:
        """Inverse of ``to_json``. Unknown top-level or per-channel fields are ignored, so a newer writer's files
        replay on an older reader; missing optional fields take their defaults (the outbox schema may grow, v2 P0)."""
        data = json.loads(text)
        channels = {
            name: ChannelState(
                state=c.get("state", "pending"),
                attempts=c.get("attempts", 0),
                next_attempt_at=datetime.fromisoformat(c["next_attempt_at"]) if c.get("next_attempt_at") else None,
                receipt=c.get("receipt"),
                last_error=c.get("last_error"),
                permanent_failures=c.get("permanent_failures", 0),
            )
            for name, c in data.get("channels", {}).items()
        }
        return cls(
            kind=data["kind"],
            severity=data["severity"],
            title=data["title"],
            body=data["body"],
            fields=data.get("fields", {}),
            commands=data.get("commands", []),
            needs_action=data.get("needs_action", False),
            dedupe_key=data.get("dedupe_key"),
            binds=data.get("binds", {}),
            alert_id=data["alert_id"],
            occurred_at=datetime.fromisoformat(data["occurred_at"]) if data.get("occurred_at") else None,
            channels=channels,
        )


def prepare(alert: Alert, now: datetime) -> Alert:
    """Stamp ``alert_id``/``occurred_at``/``channels`` in place (if unset) and return it.

    Split out of ``enqueue`` so callers inside a ``Transaction`` (which must not write outside the transaction's
    commit) can produce a ready-to-stage alert file without touching disk themselves.

    Raises ``NeedsActionError`` if ``needs_action`` is set with no commands (TS AC-04): such an alert would ask the
    user to act without telling them how.
    """
    if alert.needs_action and not alert.commands:
        raise NeedsActionError(f"{alert.kind}: needs_action alerts must carry at least one literal command")
    if alert.occurred_at is None:
        alert.occurred_at = now
    if not alert.alert_id:
        alert.alert_id = f"{alert.occurred_at.strftime('%Y%m%dT%H%M%S%f')[:16]}-{secrets.token_hex(4)}"
    if not alert.channels:
        alert.channels = {name: ChannelState() for name in DEFAULT_CHANNELS}
    return alert


def _dedupe_keys(root: Path, boxes: tuple[str, ...]) -> set[str]:
    keys: set[str] = set()
    for box in boxes:
        directory = root / "alerts" / box
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            try:
                key = json.loads(path.read_text(encoding="utf-8")).get("dedupe_key")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(key, str):
                keys.add(key)
    return keys


def pending_dedupe_keys(root: Path) -> set[str]:
    """Every ``dedupe_key`` among the undelivered alerts in ``<root>/alerts/pending`` (AS §9.2).

    Unreadable files and alerts without a key are ignored: dedup is best-effort and must never block an alert.
    """
    return _dedupe_keys(root, ("pending",))


def raised_dedupe_keys(root: Path) -> set[str]:
    """Every ``dedupe_key`` ever queued: pending, sent or dead-lettered. Answers "was the owner told about X?"."""
    return _dedupe_keys(root, ("pending", "sent", "dead"))


def enqueue(alert: Alert, store: FileStore) -> bool:
    """Write ``alert`` to ``alerts/pending/`` atomically. Stamps ``alert_id``/``occurred_at``/``channels`` if unset.

    Skips the write and returns ``False`` if the ``dedupe_key`` is already pending or held in the file-backed ledger
    (``FileStore.claim_dedupe_key``, v2 R4: independent of any database); once that alert is sent or dead-lettered
    ``notify.deliver`` releases the key and it is free again. Returns ``True`` when the alert was written.
    """
    prepare(alert, store.clock())
    if alert.dedupe_key is not None and alert.dedupe_key in pending_dedupe_keys(store.root):
        return False
    if alert.dedupe_key is not None and not store.claim_dedupe_key(alert.dedupe_key, store.clock(), DEDUPE_RETENTION):
        return False
    path = store.root / "alerts" / "pending" / f"{alert.alert_id}.json"
    atomic_write_text(path, alert.to_json())
    return True
