"""Command parsing (AS §1.7): turn the start of a message into a typed verb, target and reason.

The verb must be the first word (TS CMD-X02) so a command quoted inside prose does nothing. Proposal IDs are not
validated here beyond being passed through verbatim; format and existence checks are the handler's job so a bad ID
always gets a specific reply rather than silent non-parsing.
"""

from __future__ import annotations

import re
import unicodedata
from typing import NamedTuple

PROPOSAL_ID_RE = re.compile(r"^P-\d{4}-\d{2}-\d{2}-\d{2}$")

REASON_MAX_CHARS = 500

_VERBS = frozenset({"VETO", "APPROVE", "PAUSE", "RESUME", "CLOSE", "RECONFIRM", "RECONCILE", "STATUS", "SET"})


class ParsedCommand(NamedTuple):
    """A command parsed from the start of a message.

    ``verb`` is one of ``VETO``, ``APPROVE``, ``PAUSE``, ``RESUME``, ``CLOSE``, ``CLOSE_CONFIRM``, ``RECONFIRM``,
    ``RECONCILE``, ``STATUS``, ``SET``. ``target`` is a proposal ID, config key, or (for ``RECONCILE``) the mode
    ``"rebuild"``/``"readonly"``; ``None`` when not applicable. ``reason`` is the sanitized free text for ``VETO``
    or the raw value for ``SET``; ``None`` otherwise.
    """

    verb: str
    target: str | None
    reason: str | None


def sanitize_reason(text: str, limit: int = REASON_MAX_CHARS) -> str:
    """Strip control characters, collapse whitespace to single spaces, and clip to ``limit`` characters."""
    nfc = unicodedata.normalize("NFC", text)
    kept = "".join(c for c in nfc if unicodedata.category(c) not in ("Cc", "Cf"))
    return " ".join(kept.split())[:limit]


def parse_command(text: str) -> ParsedCommand | None:
    """Parse ``text``. Returns ``None`` if the first word is not a recognised verb or the message is empty."""
    stripped = text.strip()
    if not stripped:
        return None
    words = stripped.split(maxsplit=1)
    verb = words[0].upper()
    rest = words[1].strip() if len(words) > 1 else ""

    if verb not in _VERBS:
        return None

    if verb == "CLOSE":
        first = rest.split(maxsplit=1)[0].upper() if rest else ""
        if first == "CONFIRM":
            return ParsedCommand("CLOSE_CONFIRM", None, None)
        return ParsedCommand("CLOSE", None, None)

    if verb == "RECONCILE":
        first = rest.split(maxsplit=1)[0].lower() if rest else ""
        mode = "rebuild" if first == "rebuild" else "readonly"
        return ParsedCommand("RECONCILE", mode, None)

    if verb in ("PAUSE", "RESUME", "STATUS"):
        return ParsedCommand(verb, None, None)

    if verb == "VETO":
        parts = rest.split(maxsplit=1)
        target = parts[0] if parts else None
        reason = sanitize_reason(parts[1]) if len(parts) > 1 else None
        return ParsedCommand("VETO", target, reason)

    if verb in ("APPROVE", "RECONFIRM"):
        parts = rest.split()
        target = parts[0] if parts else None
        return ParsedCommand(verb, target, None)

    if verb == "SET":
        parts = rest.split(maxsplit=1)
        key = parts[0] if parts else None
        value = parts[1].strip() if len(parts) > 1 else None
        return ParsedCommand("SET", key, value)

    return None  # pragma: no cover - unreachable, every verb in _VERBS is handled above
