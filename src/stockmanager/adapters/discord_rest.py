"""Discord REST delivery (AS §9.2): sends alerts via the Discord HTTP API, never the gateway.

Delivery uses REST so alerts keep flowing when the bot's gateway connection is down (AS §9.1). ``httpx`` is used
directly with explicit timeouts and no following of redirects to a non-HTTPS origin.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

from stockmanager.notify.deliver import Permanent, Receipt, Rendered, Transient
from stockmanager.store.lock import Clock, now_pt

DISCORD_API_BASE = "https://discord.com/api/v10"
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
_PERMANENT_STATUSES = frozenset({400, 401, 403, 404})


class DiscordRESTChannel:
    """A ``Channel`` (AS §9.2) that posts one message per alert to a Discord channel via REST."""

    name: Literal["discord"] = "discord"

    def __init__(self, token: str, channel_id: int, *, clock: Clock = now_pt, client: httpx.Client | None = None) -> None:
        self._token = token
        self._channel_id = channel_id
        self._clock = clock
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=READ_TIMEOUT, pool=READ_TIMEOUT),
            follow_redirects=False,
            verify=True,
        )

    def send(self, rendered: Rendered, *, nonce: str) -> Receipt:
        """POST ``rendered`` to the configured channel, deduped by ``nonce`` (AS §9.2).

        Raises ``Transient`` for network errors, 5xx and 429 (``retry_after`` carried through); ``Permanent`` for
        401/403/404/400.
        """
        payload: dict[str, Any] = {"nonce": nonce, "enforce_nonce": True, "allowed_mentions": {"parse": []}}
        if rendered.embed is not None:
            payload["embeds"] = [rendered.embed]
        else:
            payload["content"] = rendered.text
        url = f"{DISCORD_API_BASE}/channels/{self._channel_id}/messages"
        headers = {"Authorization": f"Bot {self._token}", "Content-Type": "application/json"}
        try:
            response = self._client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise Transient(f"discord: timeout: {exc}") from exc
        except httpx.TransportError as exc:
            raise Transient(f"discord: transport error: {exc}") from exc
        if response.status_code == 429:
            retry_after = _retry_after(response)
            raise Transient(f"discord: rate limited (429), retry_after={retry_after}", retry_after=retry_after)
        if response.status_code >= 500:
            raise Transient(f"discord: HTTP {response.status_code}")
        if response.status_code in _PERMANENT_STATUSES:
            raise Permanent(f"discord: HTTP {response.status_code}: {response.text[:200]}")
        if response.status_code >= 400:
            raise Transient(f"discord: HTTP {response.status_code}")
        external_id = None
        try:
            external_id = str(response.json().get("id"))
        except ValueError:
            pass
        return Receipt(channel=self.name, external_id=external_id, sent_at=self._clock())

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._client.close()


def _retry_after(response: httpx.Response) -> float | None:
    try:
        body = response.json()
        if isinstance(body, dict) and "retry_after" in body:
            return float(body["retry_after"])
    except ValueError:
        pass
    header = response.headers.get("retry-after")
    if header is not None:
        try:
            return float(header)
        except ValueError:
            return None
    return None
