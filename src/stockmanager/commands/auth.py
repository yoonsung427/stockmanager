"""User authorization (AS §11.3): CLI is trusted; Discord requires an exact allow-list match."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal, NamedTuple

if TYPE_CHECKING:
    from stockmanager.commands.parser import ParsedCommand
    from stockmanager.config import Config

#: Admin operations exist only on the CLI (AS §1.7, ⚑AD10) and never appear in ``ParsedCommand.verb``; callers that
#: dispatch them (the CLI's ``admin`` sub-command) should check ``sender.kind`` directly rather than go through
#: ``authorize`` with a synthetic command, but the set is kept here so the rule lives in one place.
ADMIN_OPS: frozenset[str] = frozenset({"clear-lock", "force-evaluate", "ack-log-line", "restore"})


class Sender(NamedTuple):
    """Who issued a command and when (AS §1.7).

    ``sent_at`` is the message's own timestamp (Discord's ``created_at``, or the CLI invocation time), never
    "now" read inside the handler (TS D11): using "now" would let a long queue delay change veto-window math.
    """

    kind: Literal["discord", "cli", "assistant"]
    user_id: str | None
    message_id: str | None
    sent_at: datetime


class AuthorizationResult(NamedTuple):
    """The outcome of ``authorize``: ``ok`` and, when rejected, a human-readable ``reason``."""

    ok: bool
    reason: str | None = None


def actor(sender: Sender) -> str:
    """The audit actor recorded in hash chains: ``cli``, ``discord:<uid>`` or ``assistant:<uid>`` (v2 D2).

    An assistant-confirmed action must never chain as ``discord:<owner>``, so the kind is part of the actor.
    """
    if sender.kind == "cli":
        return "cli"
    return f"{sender.kind}:{sender.user_id}"


def authorize(sender: Sender, command: ParsedCommand, cfg: Config) -> AuthorizationResult:
    """Check authorization for ``command`` from ``sender`` (AS §11.3). Called for every command, ``STATUS`` included.

    CLI is always trusted (TS CMD-X01). Discord requires an exact string match of ``sender.user_id`` against
    ``cfg.discord.allowed_user_ids``; an empty allow-list disables Discord control entirely (TS CMD-X03). An
    ``assistant`` sender (v2 D2) is not trusted like the CLI: it passes the same allow-list check as data, with
    ``user_id`` the allow-listed user it acts for. The extra confirm-token rule is the handler's (``confirm.py``).
    """
    if sender.kind == "cli":
        return AuthorizationResult(True)
    if not cfg.discord.allowed_user_ids:
        return AuthorizationResult(False, "Discord control is disabled: no allowed_user_ids configured")
    if sender.user_id not in cfg.discord.allowed_user_ids:
        return AuthorizationResult(False, "Sender not authorized")
    return AuthorizationResult(True)
