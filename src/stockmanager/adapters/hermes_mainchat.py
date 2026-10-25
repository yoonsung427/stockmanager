"""Hermes mainchat delivery: the ``mainchat`` ``Channel`` behind the outbox drain (AS §9.2).

Sends one message per alert through the Discord REST API using Hermes' own bot token. It is one more adapter on the
single outbox drain, not a second delivery plane. HTTP, nonce and status-code behaviour is shared with
``DiscordRESTChannel`` via ``discord_rest.post_message``; errors are prefixed ``mainchat:``. The token is only ever
placed in the Authorization header and is never logged or included in a message.
"""

from __future__ import annotations

from typing import Literal

import httpx

from stockmanager.adapters.discord_rest import make_client, post_message
from stockmanager.notify.deliver import Receipt, Rendered
from stockmanager.store.lock import Clock, now_pt


class HermesMainchatChannel:
    """A ``Channel`` (AS §9.2) that posts one message per alert to the Hermes main chat channel via REST."""

    name: Literal["mainchat"] = "mainchat"

    def __init__(self, token: str, channel_id: int, *, clock: Clock = now_pt, client: httpx.Client | None = None) -> None:
        self._token = token
        self._channel_id = channel_id
        self._clock = clock
        self._client = client or make_client()

    def __repr__(self) -> str:
        return f"HermesMainchatChannel(channel_id={self._channel_id})"

    def send(self, rendered: Rendered, *, nonce: str) -> Receipt:
        """POST ``rendered`` to the mainchat channel, deduped by ``nonce`` (AS §9.2).

        Raises ``Transient`` for network errors, 5xx and 429 (``retry_after`` carried through); ``Permanent`` for
        401/403/404/400. ``Receipt.thread_id`` stays ``None``.
        """
        external_id = post_message(self._client, self._token, self._channel_id, rendered, nonce, label="mainchat")
        return Receipt(channel=self.name, external_id=external_id, sent_at=self._clock())

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._client.close()
