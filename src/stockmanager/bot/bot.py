"""Discord bot daemon (AS §9.1): transport only — turns messages into ``commands.handle(...)`` and a reply.

Business logic never lives here, so a bot crash delays replies but cannot corrupt state. The outbox deliverer runs
alongside message handling in the same process, using the REST adapter (never the gateway) so alerts still flow
when the gateway connection is down.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import discord

from stockmanager.commands import CommandContext, Sender, run_command
from stockmanager.commands.handlers import UNRECOGNIZED
from stockmanager.commands.ratelimit import RATE_LIMITED
from stockmanager.notify import enqueue, rejected_control_message
from stockmanager.notify.components import click_command, parse_custom_id
from stockmanager.notify.deliver import Channel, deliver_pending
from stockmanager.store import FileStore, fsutil
from stockmanager.store.lock import Clock, now_pt

log = logging.getLogger(__name__)

REJECTED_ALERT_WINDOW = timedelta(minutes=10)
OUTBOX_INTERVAL_S = 5.0
HEARTBEAT_INTERVAL_S = 30.0


def _channel_key(message: discord.Message) -> str:
    """The cursor key for ``message``'s channel: ``dm:<user_id>`` for a DM, else the channel's snowflake."""
    if isinstance(message.channel, discord.DMChannel):
        return f"dm:{message.author.id}"
    return str(message.channel.id)


class PortfolioBot(discord.Client):
    """Transport-only bot: turns messages into commands and runs the outbox deliverer (AS §9.1)."""

    def __init__(
        self,
        store: FileStore,
        channel_id: int,
        ctx: CommandContext,
        channels: Sequence[Channel],
        *,
        clock: Clock = now_pt,
        stock_query: Callable[[str, str], str | None] | None = None,
        **kwargs: object,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.guild_messages = True
        intents.dm_messages = True
        super().__init__(intents=intents, **kwargs)  # type: ignore[arg-type]
        self._store = store
        self._channel_id = channel_id
        self._ctx = ctx
        self._channels = list(channels)
        self._clock = clock
        self._stock_query = stock_query
        self._last_rejected_alert: dict[str, datetime] = {}
        self._outbox_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

    # -- state paths -------------------------------------------------------------------------------------------

    @property
    def _cursor_path(self) -> Path:
        return self._store.root / "state" / "bot_cursor.json"

    @property
    def _heartbeat_path(self) -> Path:
        return self._store.root / "state" / "bot_heartbeat.json"

    def _read_cursor(self) -> dict[str, int]:
        if not self._cursor_path.exists():
            return {}
        data: dict[str, int] = json.loads(self._cursor_path.read_text(encoding="utf-8"))
        return data

    def _write_cursor(self, channel_key: str, message_id: int) -> None:
        cursor = self._read_cursor()
        cursor[channel_key] = message_id
        fsutil.atomic_write_text(self._cursor_path, json.dumps(cursor, indent=2) + "\n")

    def _should_alert_rejection(self, key: str, now: datetime) -> bool:
        """``rejected_control_message`` alerts are limited to one per sender per 10 min (⚑AD11)."""
        last = self._last_rejected_alert.get(key)
        if last is not None and now - last < REJECTED_ALERT_WINDOW:
            return False
        self._last_rejected_alert[key] = now
        return True

    # -- message handling ----------------------------------------------------------------------------------------

    def _is_allowed_channel(self, message: discord.Message) -> bool:
        """The allowed channel, a DM, or a thread whose parent is the allowed channel (v2 H5: proposal threads)."""
        channel = message.channel
        if isinstance(channel, discord.Thread):
            return channel.parent_id == self._channel_id
        return isinstance(channel, discord.DMChannel) or channel.id == self._channel_id

    async def on_message(self, message: discord.Message) -> None:
        """Turn one incoming message into a reply (AS §9.1)."""
        if message.author.bot:
            return
        if not self._is_allowed_channel(message):
            return
        await self._process_message(message)

    async def _process_message(self, message: discord.Message) -> None:
        """The shared path for live messages and backfilled ones."""
        sender = Sender("discord", str(message.author.id), str(message.id), message.created_at)
        key = str(message.author.id)
        now = self._clock()
        reply = await asyncio.to_thread(run_command, message.content, sender, self._ctx)
        if reply == RATE_LIMITED:  # the handler layer owns the budget (v2 D2); the bot only relays the refusal
            await self._reply(message, reply)
            return
        answered = False
        if reply == UNRECOGNIZED and self._stock_query is not None:
            # "what's going on with MSFT?": not a command, so try it as a stock query. It does the work (an LLM
            # call) off the event loop and owns authorization; ``None`` keeps the rejection above.
            stock_reply = await asyncio.to_thread(self._stock_query, message.content, key)
            if stock_reply is not None:
                reply, answered = stock_reply, True
        if not answered and reply.startswith("❌") and self._should_alert_rejection(key, now):
            await asyncio.to_thread(enqueue, rejected_control_message(key), self._store)
        await self._reply(message, reply)
        self._write_cursor(_channel_key(message), message.id)

    # -- proposal buttons (v2 H4) ------------------------------------------------------------------------------------

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """A click on a proposal embed's VETO / APPROVE button: the same handler as the typed command (v2 H4).

        Anything that is not one of our buttons is left to discord.py. The reply is ephemeral, so a rejection never
        lands in the shared channel, and it is always sent: a click is never silently dropped.
        """
        if interaction.type is not discord.InteractionType.component:
            return
        data = cast("dict[str, Any]", interaction.data or {})
        custom_id = str(data.get("custom_id", ""))
        if parse_custom_id(custom_id) is None:
            return
        await interaction.response.defer(ephemeral=True)
        reply = await self.handle_component(
            custom_id, str(interaction.user.id), str(interaction.id), interaction.created_at
        )
        await interaction.followup.send(
            reply or UNRECOGNIZED, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    async def handle_component(
        self, custom_id: str, user_id: str, interaction_id: str, at: datetime, reason: str | None = None
    ) -> str | None:
        """Resolve one button click as ``commands.run_command`` would its typed twin; ``None`` if not our button.

        ``sender`` is ``discord`` with the clicker's ID and the click's own timestamp (TS D11), so ``authorize()``
        rejects a non-allow-list clicker with the identical reason text as a typed command, and the proposal's
        transition row records ``discord:<user_id>``, ``at`` and ``reason`` (``VETO`` only; the default cause is
        "vetoed"). The same rate limit and rejection alert apply as for typed commands.

        Bot-down ruling: the buttons are persistent (see ``notify.components``). A click made while no bot is
        running is not queued or replayed; Discord shows the clicker its own failure notice and nothing changes.
        """
        parsed = parse_custom_id(custom_id)
        if parsed is None:
            return None
        action, proposal_id = parsed
        sender = Sender("discord", user_id, interaction_id, at)
        now = self._clock()
        reply = await asyncio.to_thread(run_command, click_command(action, proposal_id, reason), sender, self._ctx)
        if reply == RATE_LIMITED:  # the handler layer owns the budget (v2 D2); the bot only relays the refusal
            return reply
        if reply.startswith("❌") and self._should_alert_rejection(user_id, now):
            await asyncio.to_thread(enqueue, rejected_control_message(user_id), self._store)
        return reply

    @staticmethod
    async def _reply(message: discord.Message, text: str) -> None:
        """Every reply uses ``allowed_mentions=none`` (TS D11): an echoed veto reason can never ping anyone."""
        await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    # -- lifecycle -------------------------------------------------------------------------------------------------

    async def on_ready(self) -> None:
        """Backfill missed messages, then start the outbox and heartbeat loops (AS §9.1)."""
        await self._backfill()
        self._start_background_loops()

    async def on_resumed(self) -> None:
        """The gateway does not replay missed messages; backfill from the cursor exactly as on connect."""
        await self._backfill()
        self._start_background_loops()

    def _start_background_loops(self) -> None:
        if self._outbox_task is None or self._outbox_task.done():
            self._outbox_task = asyncio.create_task(self._outbox_loop())
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _backfill(self) -> None:
        """Fetch and process messages sent while disconnected (AS §9.1); the cursor dedupe makes replays harmless."""
        cursor = self._read_cursor()
        channel = self.get_channel(self._channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self._channel_id)
            except discord.HTTPException:
                channel = None
        if isinstance(channel, discord.TextChannel | discord.Thread):
            await self._backfill_history(channel, str(self._channel_id), cursor)
        for user_id in self._ctx.cfg.discord.allowed_user_ids:
            try:
                user = await self.fetch_user(int(user_id))
                dm = user.dm_channel or await user.create_dm()
            except (discord.HTTPException, ValueError):
                continue
            await self._backfill_history(dm, f"dm:{user_id}", cursor)

    async def _backfill_history(
        self, channel: discord.TextChannel | discord.Thread | discord.DMChannel, key: str, cursor: dict[str, int]
    ) -> None:
        after_id = cursor.get(key)
        after = discord.Object(id=after_id) if after_id else None
        async for message in channel.history(after=after, oldest_first=True, limit=None):
            if message.author.bot:
                continue
            await self._process_message(message)

    async def _outbox_loop(self) -> None:
        """Every 5 s: deliver pending alerts via the REST adapter, off the event loop (AS §9.1)."""
        while not self.is_closed():
            await self._deliver_once()
            await asyncio.sleep(OUTBOX_INTERVAL_S)

    async def _deliver_once(self) -> None:
        """One outbox delivery pass; split out of ``_outbox_loop`` so it can be exercised without the loop."""
        try:
            await asyncio.to_thread(deliver_pending, self._store, self._channels, self._clock)
        except Exception:
            log.exception("outbox delivery failed")

    async def _heartbeat_loop(self) -> None:
        """Every 30 s: write ``state/bot_heartbeat.json`` (AS §9.1)."""
        while not self.is_closed():
            await self._write_heartbeat()
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    async def _write_heartbeat(self) -> None:
        """One heartbeat write; split out of ``_heartbeat_loop`` so it can be exercised without the loop."""
        payload = json.dumps({"at": self._clock().isoformat()}, indent=2) + "\n"
        await asyncio.to_thread(fsutil.atomic_write_text, self._heartbeat_path, payload)

    async def close(self) -> None:
        """``SIGTERM`` finishes the in-flight handler and exits; the cursor guarantees no command is lost."""
        if self._outbox_task is not None:
            self._outbox_task.cancel()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
        await super().close()
