"""Composition root: the only place that wires ports to real or simulated adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]


async def app(
    scope: Scope,
    receive: Callable[[], Awaitable[Message]],
    send: Callable[[Message], Awaitable[None]],
) -> None:
    """Placeholder ASGI app so ``make dev`` has something to serve; replace when the dashboard lands."""
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"stockmanager: ok\n"})
