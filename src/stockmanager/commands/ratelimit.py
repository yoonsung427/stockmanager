"""Command rate limit (AS §9.1, v2 D2): one budget per sender, drawn on by every path that reaches the handlers.

It lives in the handler layer so typed Discord messages, assistant commands and the CLI all spend the same 10 per
minute: the assistant plane cannot dodge the limit by being a different transport, and the CLI is no longer exempt.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stockmanager.commands.auth import Sender

RATE_LIMIT_WINDOW = timedelta(minutes=1)
RATE_LIMIT_MAX = 10

#: The reply for a command dropped by the limiter; the bot keys off it to skip the rejected-message alert.
RATE_LIMITED = "❌ Rate limited: too many commands, try again in a minute."


def limiter_key(sender: Sender) -> str:
    """The budget a sender draws on: the user id for ``discord`` and ``assistant`` (one shared pool), else ``cli``."""
    if sender.kind == "cli":
        return "cli"
    return sender.user_id or sender.kind


class RateLimiter:
    """In-memory sliding window: at most ``maximum`` commands per ``window`` per key."""

    def __init__(self, window: timedelta = RATE_LIMIT_WINDOW, maximum: int = RATE_LIMIT_MAX) -> None:
        self._window = window
        self._maximum = maximum
        self._recent: dict[str, deque[datetime]] = defaultdict(deque)

    def limited(self, key: str, now: datetime) -> bool:
        """Record one command for ``key`` at ``now``; ``True`` if it exceeds the budget (and is then not counted)."""
        recent = self._recent[key]
        while recent and now - recent[0] > self._window:
            recent.popleft()
        if len(recent) >= self._maximum:
            return True
        recent.append(now)
        return False
