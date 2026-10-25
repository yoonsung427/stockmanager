"""Assistant confirm tokens and the handler-side gate (v2 D2).

The assistant may only *propose* a state-changing command. Execution needs a confirm token signed by the bot (HMAC
over the exact command text, the user it acts for, a single-use ref and an expiry) that ``Bridge.confirm`` issues
only after the echo was delivered and the user acted. ``handlers.handle`` calls ``check_assistant`` for every
``assistant`` sender, so the rule holds for any caller, not just the bridge.

Which verbs skip the token is an allow-list (``READ_VERBS`` plus ``INSTANT_VERBS``), not a deny-list: a verb added
to the parser later needs a confirm token until someone deliberately lists it here.

The signing key comes from the environment, is wrapped so ``repr``/``str`` never show it, and is never logged.
D2 also requires the assistant plane to run as a separate OS user with a read/propose-only API and no access to
this key; that provisioning is out of scope for the repo and must be done by the deployment.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from stockmanager.commands.auth import Sender
    from stockmanager.commands.handlers import CommandContext
    from stockmanager.commands.parser import ParsedCommand

ENV_KEY = "STOCKMANAGER_BRIDGE_KEY"
MIN_KEY_BYTES = 32
TOKEN_TTL = timedelta(minutes=10)

#: Verbs with no side effects: the assistant may invoke them directly.
READ_VERBS: frozenset[str] = frozenset({"STATUS"})
#: Verbs that only reduce risk: allowed instantly. ``PAUSE`` cancels open proposals and stops fills.
INSTANT_VERBS: frozenset[str] = frozenset({"PAUSE"})

_REF_RE = re.compile(r"^[0-9a-f]{16}$")


class BridgeKeyError(RuntimeError):
    """The bridge signing key is missing or too short."""


class TokenError(ValueError):
    """A confirm token was rejected; the message is safe to show the user."""


class SigningKey:
    """The bot's token-signing secret. Its ``repr`` is redacted so it cannot leak through logs or tracebacks."""

    __slots__ = ("_secret",)

    def __init__(self, secret: bytes) -> None:
        if len(secret) < MIN_KEY_BYTES:
            raise BridgeKeyError(f"the bridge signing key must be at least {MIN_KEY_BYTES} bytes")
        self._secret = secret

    def sign(self, message: bytes) -> bytes:
        """HMAC-SHA256 of ``message``."""
        return hmac.new(self._secret, message, hashlib.sha256).digest()

    def __repr__(self) -> str:
        return "SigningKey(<redacted>)"


def load_key(env: Mapping[str, str] | None = None) -> SigningKey:
    """The signing key from ``STOCKMANAGER_BRIDGE_KEY`` (``env`` defaults to ``os.environ``)."""
    raw = (os.environ if env is None else env).get(ENV_KEY)
    if not raw:
        raise BridgeKeyError(f"{ENV_KEY} is not set")
    return SigningKey(raw.encode("utf-8"))


def requires_confirm(verb: str) -> bool:
    """True unless ``verb`` is allow-listed for direct assistant use."""
    return verb not in READ_VERBS and verb not in INSTANT_VERBS


# -- tokens -------------------------------------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(key: SigningKey, *, ref: str, uid: str, command: str, confirmed_by: str, now: datetime) -> str:
    """A signed, expiring token for exactly ``command`` on behalf of ``uid``, confirmed by ``confirmed_by``."""
    claims = {"ref": ref, "uid": uid, "cmd": command, "by": confirmed_by, "exp": (now + TOKEN_TTL).isoformat()}
    body = _b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{_b64(key.sign(body.encode('ascii')))}"


def verify_token(key: SigningKey, token: str, *, command: str, uid: str, now: datetime) -> dict[str, str]:
    """The claims of ``token`` if it is authentic, unexpired and bound to ``command`` and ``uid``; else ``TokenError``."""
    try:
        body, sig = token.split(".")
        supplied = _unb64(sig)
        raw = json.loads(_unb64(body))
    except ValueError as exc:
        raise TokenError("malformed confirm token") from exc
    if not hmac.compare_digest(supplied, key.sign(body.encode("ascii"))):
        raise TokenError("confirm token signature is invalid")
    if not isinstance(raw, dict):
        raise TokenError("malformed confirm token")
    claims = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
    if not {"ref", "uid", "cmd", "by", "exp"} <= claims.keys():
        raise TokenError("malformed confirm token")
    try:
        expires = datetime.fromisoformat(claims["exp"])
    except ValueError as exc:
        raise TokenError("malformed confirm token") from exc
    if now >= expires:
        raise TokenError("confirm token expired; ask the assistant to propose again")
    if claims["cmd"] != command or claims["uid"] != uid:
        raise TokenError("confirm token was issued for a different command")
    return claims


# -- single-use refs and the decision record ---------------------------------------------------------------------


def bridge_dir(root: Path) -> Path:
    """``<workspace>/state/bridge``: proposal records and the used-ref markers."""
    return root / "state" / "bridge"


def record_path(root: Path, ref: str) -> Path:
    """The decision record for ``ref``; ``ref`` must be 16 lowercase hex characters."""
    if not _REF_RE.fullmatch(ref):
        raise ValueError(f"invalid bridge ref: {ref!r}")
    return bridge_dir(root) / f"{ref}.json"


def read_record(root: Path, ref: str) -> dict[str, Any] | None:
    """The record for ``ref``, or ``None`` if it is missing, unreadable or the ref is malformed."""
    try:
        data = json.loads(record_path(root, ref).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_consumed(root: Path, ref: str) -> bool:
    """True once ``ref``'s token has been spent."""
    return _REF_RE.fullmatch(ref) is not None and (bridge_dir(root) / "used" / ref).exists()


def consume_ref(root: Path, ref: str) -> bool:
    """Spend ``ref`` exactly once (atomic ``O_EXCL`` create); ``False`` if malformed or already spent."""
    if not _REF_RE.fullmatch(ref):
        return False
    used = bridge_dir(root) / "used"
    used.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(used / ref, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    os.close(fd)
    return True


# -- handler-side enforcement -----------------------------------------------------------------------------------


def check_assistant(parsed: ParsedCommand, text: str, sender: Sender, token: str | None, ctx: CommandContext) -> str | None:
    """Gate an ``assistant`` sender's command: ``None`` to proceed, else the refusal reason.

    Allow-listed verbs pass. Anything else needs a valid, unspent token bound to ``text`` and the sender's user id;
    a valid token is spent here, so it cannot be replayed. With no signing key configured it fails closed.
    """
    if not requires_confirm(parsed.verb):
        return None
    if ctx.bridge_key is None:
        return "Assistant commands that change state are unavailable: no bridge signing key is configured"
    if token is None:
        return f"{parsed.verb} needs your confirmation; the assistant can only propose it"
    try:
        claims = verify_token(ctx.bridge_key, token, command=text, uid=sender.user_id or "", now=ctx.clock())
    except TokenError as exc:
        return str(exc)
    if sender.message_id != claims["ref"]:
        return "confirm token does not belong to this dispatch"
    if not consume_ref(ctx.store.root, claims["ref"]):
        return "confirm token was already used"
    return None


def annotate_cause(root: Path, sender: Sender, cause: str) -> str:
    """``cause`` plus, for an assistant sender, the confirm ref and the user who confirmed it (v2 D2 audit).

    Written into the proposal chain next to the ``assistant:<uid>`` actor, so the confirm -> action decision survives
    even when the SQLite event log is down.
    """
    if sender.kind != "assistant":
        return cause
    if not sender.message_id:
        return f"{cause} [assistant, direct]"
    record = read_record(root, sender.message_id)
    by = record.get("confirmed_by") if record else None
    return f"{cause} [assistant-confirmed ref={sender.message_id} by={by}]"
