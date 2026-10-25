"""The assistant bridge (v2 D2): the assistant proposes, the user confirms, the handlers run it unchanged.

Flow for a state-changing command: ``propose`` writes a proposal record and queues an echo of the exact command
text through the outbox; once that echo has been delivered to Discord, ``confirm`` (called when the user clicks or
types the echoed command) issues a bot-signed token; ``dispatch`` then runs the command through ``run_command``
(parser + ``authorize`` unchanged) as an ``assistant`` sender, where ``confirm.check_assistant`` verifies and spends
the token. The assistant cannot confirm for itself: ``confirm`` refuses ``assistant`` senders, and the token needs
the bot's key, which the assistant plane never holds (D2: the OS-user / process boundary that enforces that is
deployment work and is not provisioned here).

Allow-listed verbs (``confirm.READ_VERBS``, ``INSTANT_VERBS``) skip the token and run at once.

Everything the assistant says goes through the outbox (``notify.enqueue``, the single writer, C5). Each decision
(proposed text, confirming user, final command, outcome) is recorded in ``state/bridge/<ref>.json``; the
state-change itself carries ``assistant:<uid>`` in the proposal chain (``handlers``), so audit never depends on SQLite.
"""

from __future__ import annotations

import json
import secrets
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from stockmanager.commands.auth import Sender, actor, authorize
from stockmanager.commands.confirm import (
    TOKEN_TTL,
    SigningKey,
    bridge_dir,
    is_consumed,
    issue_token,
    read_record,
    record_path,
    requires_confirm,
)
from stockmanager.commands.handlers import UNRECOGNIZED, run_command
from stockmanager.commands.parser import parse_command
from stockmanager.commands.ratelimit import RATE_LIMITED, limiter_key
from stockmanager.notify.outbox import Alert, ChannelState, enqueue
from stockmanager.store import fsutil

if TYPE_CHECKING:
    from stockmanager.commands.handlers import CommandContext

CONTEXT_TAG = "assistant_context"
REPLY_MAX_CHARS = 4000


class BridgeRefused(Exception):
    """A confirm or dispatch was refused; the message is safe to show the user."""


@dataclass(frozen=True)
class BridgeResult:
    """What ``Bridge.propose`` did: ran an allow-listed command, queued a proposal, or refused."""

    status: Literal["executed", "proposed", "refused"]
    reply: str
    ref: str | None = None


class ContextEntry(NamedTuple):
    """One piece of material shown to the assistant: where it came from and its (untrusted) text."""

    source: str
    text: str


def render_assistant_context(entries: Sequence[ContextEntry]) -> str:
    """Render ``entries`` as one data block and nothing else (C3: untrusted content is data, not instruction).

    The payload is JSON with ASCII-only escapes, and ``<``/``>`` are escaped too, so no entry can add a line, fake a
    closing tag or smuggle look-alike characters out of the block.
    """
    payload = [{"source": e.source, "text": e.text} for e in entries]
    body = json.dumps(payload, indent=2, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e")
    return f'<{CONTEXT_TAG} format="json" trust="untrusted-data">\n{body}\n</{CONTEXT_TAG}>'


def _has_control_chars(text: str) -> bool:
    return any(unicodedata.category(c) in ("Cc", "Cf") for c in text)


class Bridge:
    """Propose / confirm / dispatch for assistant commands, bound to one ``CommandContext`` and its signing key."""

    def __init__(self, ctx: CommandContext, key: SigningKey) -> None:
        self._ctx = ctx
        self._key = key

    # -- propose ---------------------------------------------------------------------------------------------------

    def propose(self, text: str, uid: str) -> BridgeResult:
        """The assistant's entry point for ``text`` on behalf of allow-listed user ``uid``.

        An allow-listed verb runs now; anything else is only echoed to the user and recorded. The reply is queued
        through the outbox either way. A proposal also spends one unit of the shared command budget.
        """
        ctx = self._ctx
        now = ctx.clock()
        command = text.strip()
        sender = Sender("assistant", uid, None, now)
        parsed = None if _has_control_chars(command) else parse_command(command)
        if parsed is None:
            return self._refused(UNRECOGNIZED)
        auth = authorize(sender, parsed, ctx.cfg)
        if not auth.ok:
            return self._refused(f"❌ {auth.reason}")
        if not requires_confirm(parsed.verb):
            reply = run_command(command, sender, ctx)
            self._reply(reply)
            return BridgeResult("executed", reply)
        if ctx.rate_limiter.limited(limiter_key(sender), now):
            return self._refused(RATE_LIMITED)
        ref = secrets.token_hex(8)
        echo = Alert(
            kind="assistant_proposal",
            severity="info",
            title=f"Assistant proposes: {parsed.verb}",
            body="The assistant wants to run the command below for you. It does nothing until you confirm it.",
            fields={"ref": ref, "for": uid},
            commands=[command],
            needs_action=True,
            dedupe_key=f"assistant_proposal:{ref}",
            binds={"bridge_ref": ref},
            channels={"discord": ChannelState()},  # the user must see it in the channel; nothing else counts
        )
        enqueue(echo, ctx.store)
        self._write(
            ref,
            {
                "ref": ref,
                "state": "proposed",
                "uid": uid,
                "command": command,
                "proposed_at": now.isoformat(),
                "echo_alert_id": echo.alert_id,
                "llm_mediated": True,
            },
        )
        reply = f"Proposed `{command}`; waiting for your confirmation."
        return BridgeResult("proposed", reply, ref)

    # -- confirm ---------------------------------------------------------------------------------------------------

    def confirm(self, ref: str, confirmer: Sender) -> str:
        """Issue the confirm token for proposal ``ref`` once ``confirmer`` (a button click or typed echo) acts.

        Refused unless: the confirmer is not the assistant, is authorized for the command (allow-list), is the user
        the proposal is for, the echo was actually delivered to Discord, and the proposal is fresh and unconfirmed.
        """
        ctx = self._ctx
        if confirmer.kind == "assistant":
            raise BridgeRefused("The assistant cannot confirm its own proposal")
        record = read_record(ctx.store.root, ref)
        if record is None:
            raise BridgeRefused("Unknown proposal")
        if record.get("state") != "proposed":
            raise BridgeRefused(f"Proposal is already {record.get('state')}")
        command = str(record["command"])
        parsed = parse_command(command)
        if parsed is None:  # pragma: no cover - propose only records parseable commands
            raise BridgeRefused(UNRECOGNIZED)
        auth = authorize(confirmer, parsed, ctx.cfg)
        if not auth.ok:
            raise BridgeRefused(f"{auth.reason}")
        if confirmer.kind == "discord" and confirmer.user_id != record["uid"]:
            raise BridgeRefused("Only the user this proposal is for can confirm it")
        if not self._echo_delivered(str(record["echo_alert_id"])):
            raise BridgeRefused("The proposal has not been shown to you yet")
        if confirmer.sent_at - datetime.fromisoformat(str(record["proposed_at"])) > TOKEN_TTL:
            raise BridgeRefused("Proposal expired; ask the assistant to propose again")
        confirmed_by = actor(confirmer)
        token = issue_token(
            self._key, ref=ref, uid=str(record["uid"]), command=command, confirmed_by=confirmed_by, now=ctx.clock()
        )
        confirmed = {**record, "state": "confirmed", "confirmed_by": confirmed_by}
        self._write(ref, {**confirmed, "confirmed_at": confirmer.sent_at.isoformat()})
        return token

    def confirm_typed(self, text: str, confirmer: Sender) -> str | None:
        """Confirm the open proposal whose echoed command is exactly ``text``; its token, or ``None`` if none matches."""
        command = text.strip()
        directory = bridge_dir(self._ctx.store.root)
        if not directory.exists():
            return None
        for path in sorted(directory.glob("*.json")):
            record = read_record(self._ctx.store.root, path.stem)
            if record and record.get("state") == "proposed" and record.get("command") == command:
                return self.confirm(path.stem, confirmer)
        return None

    # -- dispatch --------------------------------------------------------------------------------------------------

    def dispatch(self, ref: str, token: str) -> str:
        """Run the confirmed command through ``run_command`` as ``assistant:<uid>`` and queue the reply.

        The decision record is finalised only if the handler accepted (and so spent) the token: a refused or
        rate-limited dispatch leaves the proposal ``confirmed`` and retryable until the token expires.
        """
        ctx = self._ctx
        record = read_record(ctx.store.root, ref)
        if record is None or record.get("state") != "confirmed":
            raise BridgeRefused("Proposal is not confirmed")
        command = str(record["command"])
        sender = Sender("assistant", str(record["uid"]), ref, datetime.fromisoformat(str(record["confirmed_at"])))
        reply = run_command(command, sender, ctx, confirm_token=token)
        if is_consumed(ctx.store.root, ref):
            self._write(
                ref,
                {
                    **record,
                    "state": "dispatched",
                    "final_command": command,
                    "actor": actor(sender),
                    "outcome": "ok" if reply.startswith("✅") else "refused",
                    "reply": reply,
                    "dispatched_at": ctx.clock().isoformat(),
                },
            )
        self._reply(reply)
        return reply

    # -- helpers ---------------------------------------------------------------------------------------------------

    def _refused(self, reply: str) -> BridgeResult:
        self._reply(reply)
        return BridgeResult("refused", reply)

    def _reply(self, text: str) -> None:
        """Queue an assistant message. The outbox is the only writer to Discord (C5); the bridge never sends."""
        enqueue(
            Alert(
                kind="assistant_reply",
                severity="info",
                title="Assistant",
                body=text[:REPLY_MAX_CHARS],
                fields={},
                commands=[],
                needs_action=False,
                dedupe_key=None,
                binds={},
            ),
            self._ctx.store,
        )

    def _write(self, ref: str, record: dict[str, Any]) -> None:
        text = json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        fsutil.atomic_write_text(record_path(self._ctx.store.root, ref), text)

    def _echo_delivered(self, alert_id: str) -> bool:
        """True once the echo alert has been sent on the ``discord`` channel (read back from the outbox, not trusted)."""
        for box in ("sent", "pending"):
            path = self._ctx.store.root / "alerts" / box / f"{alert_id}.json"
            if not path.exists():
                continue
            try:
                state = Alert.from_json(path.read_text(encoding="utf-8")).channels.get("discord")
            except (OSError, ValueError, KeyError):
                return False
            return state is not None and state.state == "sent"
        return False
