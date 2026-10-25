"""Durable preference constraints (v2 S4, owner decision 2026-10-24 on R1/D6): ``workspace/preferences.json``.

A constraint is a fixed-schema entry ``{kind, ticker, scope, severity, set_at, source}`` that the reasoner input
carries on every evaluation cycle (``evaluation/input_builder``). Every field is enumerated or pattern-checked; there
is NO free-text field, because a stored free-text preference would be a persistent prompt-injection channel into the
reasoner. The write grammar is four tokens, so prose cannot be smuggled in either::

    SET prefs <kind> <ticker> <scope> <severity>
    SET prefs remove <kind> <ticker> <scope>

* ``kind``: a triage ``EventKind`` (the event type the constraint is about, ``triage/prefilter.py``).
* ``scope``: which trade action it constrains: ``buy``, ``sell`` or ``any``.
* ``severity``: the notification ``Severity`` vocabulary (``notify/outbox.py``) as weight: ``alert`` is the owner's
  strongest signal, ``info`` the softest.
* ``source``: who set it. ``owner`` is a direct ``SET`` from the CLI or an allow-listed Discord user; ``bridge`` is
  an assistant-proposed entry that the owner confirmed (``commands/confirm.py``). Both reach the file only through
  ``SET`` in ``commands/handlers.py``, inside a store transaction; the assistant has no other write path.

Constraints are advisory input to the LLM. The hard limits (position, sector and trade caps) stay in code and are not
affected by anything stored here. A missing or unparseable file reads as "no constraints" rather than halting the
evaluation; invalid entries are dropped individually.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from stockmanager.notify.outbox import Severity
from stockmanager.triage.models import EventKind

PREFS_FILENAME = "preferences.json"
#: Constraints rendered to the reasoner per cycle, newest first.
MAX_PREFS = 10
#: Constraints kept on disk; adding past it drops the oldest.
MAX_STORED_PREFS = 50

Scope = Literal["buy", "sell", "any"]
Source = Literal["owner", "bridge"]
#: Uppercase symbol with an optional ``.``/``-`` class suffix (``BRK.B``); 1-5 letters or digits, no whitespace.
Ticker = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9]{0,4}([.-][A-Z]{1,2})?$")]
_SCOPES = ("buy", "sell", "any")
_SEVERITIES = ("info", "positive", "alert")


class PrefError(ValueError):
    """The ``SET prefs`` value is not a valid constraint; the message is safe to show the user."""


class Preference(BaseModel):
    """One stored constraint. ``extra="forbid"`` and enumerated fields: nothing outside the schema is representable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EventKind
    ticker: Ticker
    scope: Scope
    severity: Severity
    set_at: datetime
    source: Source

    @property
    def key(self) -> tuple[str, str, str]:
        """``(kind, ticker, scope)``: setting the same key again replaces the entry."""
        return (self.kind.value, self.ticker, self.scope)


class _PrefsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    preferences: list[Preference] = Field(default_factory=list)


def load_prefs(root: Path) -> list[Preference]:
    """Stored constraints in file order (oldest first); ``[]`` if the file is absent or unparseable.

    Entries that fail the schema are skipped one by one, so a hand-edited bad entry never reaches the reasoner.
    """
    try:
        raw = json.loads((root / PREFS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = raw.get("preferences") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []
    out: list[Preference] = []
    for entry in entries:
        try:
            out.append(Preference.model_validate(entry))
        except ValidationError:
            continue
    return out


def dump_prefs(prefs: list[Preference]) -> str:
    """Canonical JSON (AS §3.0: ``indent=2``, ``ensure_ascii=False``, trailing newline) for the whole file."""
    return json.dumps(_PrefsFile(preferences=prefs).model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"


def active_prefs(prefs: list[Preference], limit: int = MAX_PREFS) -> list[Preference]:
    """The ``limit`` newest constraints, newest first (ties keep the later-stored one first)."""
    ranked = sorted(enumerate(prefs), key=lambda p: (p[1].set_at, p[0]), reverse=True)
    return [p for _, p in ranked[:limit]]


def apply_set(prefs: list[Preference], value: str, *, now: datetime, source: Source) -> list[Preference]:
    """``prefs`` after the ``SET prefs`` ``value``; raises ``PrefError`` for anything outside the grammar.

    ``<kind> <ticker> <scope> <severity>`` adds (or replaces the same ``kind``/``ticker``/``scope``);
    ``remove <kind> <ticker> <scope>`` deletes. Exactly the expected tokens, each checked against its enumeration or
    pattern, so free text is rejected rather than truncated or stored.
    """
    tokens = value.split()
    if tokens[:1] == ["remove"]:
        if len(tokens) != 4:
            raise PrefError("usage: SET prefs remove <kind> <ticker> <scope>")
        target = _checked(tokens[1], tokens[2].upper(), tokens[3], "info", now, source).key
        return [p for p in prefs if p.key != target]
    if len(tokens) != 4:
        raise PrefError("usage: SET prefs <kind> <ticker> <scope> <severity>")
    new = _checked(tokens[0], tokens[1].upper(), tokens[2], tokens[3], now, source)
    kept = [p for p in prefs if p.key != new.key]
    return [*kept, new][-MAX_STORED_PREFS:]


def _checked(kind: str, ticker: str, scope: str, severity: str, now: datetime, source: Source) -> Preference:
    if scope not in _SCOPES:
        raise PrefError(f"scope must be one of {', '.join(_SCOPES)}")
    if severity not in _SEVERITIES:
        raise PrefError(f"severity must be one of {', '.join(_SEVERITIES)}")
    try:
        return Preference.model_validate(
            {"kind": kind, "ticker": ticker, "scope": scope, "severity": severity, "set_at": now, "source": source}
        )
    except ValidationError as exc:
        field = str(exc.errors()[0]["loc"][0])
        allowed = f" (one of {', '.join(e.value for e in EventKind)})" if field == "kind" else ""
        raise PrefError(f"invalid {field}{allowed}") from exc
