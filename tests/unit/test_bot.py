"""The Discord bot (AS §9.1): message handling, rate limits, cursor, outbox and heartbeat loops."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from stockmanager.bot.bot import PortfolioBot
from stockmanager.commands import CommandContext
from stockmanager.config import Config, DiscordConfig
from stockmanager.domain.docs import PriceQuote
from stockmanager.notify import enqueue
from stockmanager.notify.deliver import Receipt, Rendered
from stockmanager.store import FileStore
from tests.support.books import FakePidChecker, FixedClock

PT = ZoneInfo("America/Los_Angeles")
CHANNEL_ID = 555


class FakeCalendar:
    def next_session_open_at_or_after(self, instant: datetime) -> datetime:
        return instant


class FakePriceProvider:
    name: Literal["yahoo", "finnhub"] = "yahoo"

    def get_price(self, ticker: str, session_date: object, kind: object) -> PriceQuote:  # pragma: no cover - unused
        raise NotImplementedError

    def prime(  # pragma: no cover - unused
        self, tickers: Sequence[str], session_date: date, kind: Literal["open", "close"]
    ) -> dict[str, Exception]:
        return {}


class FakeDeliveryChannel:
    name: Literal["discord", "mainchat"] = "mainchat"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, rendered: Rendered, *, nonce: str) -> Receipt:
        self.sent.append(nonce)
        return Receipt(channel=self.name, external_id=None, sent_at=datetime.now(PT))


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def store(tmp_path: Path, clock: FixedClock) -> FileStore:
    return FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)


@pytest.fixture
def ctx(store: FileStore, clock: FixedClock) -> CommandContext:
    cfg = Config(discord=DiscordConfig(allowed_user_ids=["1", "2"], channel_id=str(CHANNEL_ID)))
    return CommandContext(store=store, cfg=cfg, clock=clock, calendar=FakeCalendar(), price_provider=FakePriceProvider())


@pytest.fixture
def bot(store: FileStore, ctx: CommandContext, clock: FixedClock) -> PortfolioBot:
    return PortfolioBot(store, CHANNEL_ID, ctx, [FakeDeliveryChannel()], clock=clock)


def make_channel(channel_id: int, is_dm: bool = False) -> MagicMock:
    cls = discord.DMChannel if is_dm else discord.TextChannel
    channel = MagicMock(spec=cls)
    channel.id = channel_id
    return channel


def make_message(
    *, channel: MagicMock, author_id: int = 1, author_is_bot: bool = False, content: str = "STATUS", message_id: int = 100
) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.channel = channel
    message.author = MagicMock()
    message.author.bot = author_is_bot
    message.author.id = author_id
    message.id = message_id
    message.content = content
    message.created_at = datetime(2026, 10, 12, 9, 0, tzinfo=PT)
    message.reply = AsyncMock()
    return message


# -- message handling ----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_message_in_allowed_channel_replies_and_writes_cursor(bot: PortfolioBot) -> None:
    channel = make_channel(CHANNEL_ID)
    message = make_message(channel=channel, author_id=1, content="STATUS")
    await bot.on_message(message)
    message.reply.assert_awaited_once()
    args, kwargs = message.reply.call_args
    assert args[0].startswith("✅")
    assert kwargs["mention_author"] is False
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    cursor = json.loads(bot._cursor_path.read_text())
    assert cursor[str(CHANNEL_ID)] == 100


@pytest.mark.asyncio
async def test_on_message_in_other_channel_is_ignored(bot: PortfolioBot) -> None:
    channel = make_channel(999)
    message = make_message(channel=channel)
    await bot.on_message(message)
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_in_dm_is_processed(bot: PortfolioBot) -> None:
    channel = make_channel(42, is_dm=True)
    message = make_message(channel=channel, author_id=1)
    await bot.on_message(message)
    message.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_message_is_skipped(bot: PortfolioBot) -> None:
    channel = make_channel(CHANNEL_ID)
    message = make_message(channel=channel, author_is_bot=True)
    await bot.on_message(message)
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowed_mentions_none_on_every_reply(bot: PortfolioBot) -> None:
    channel = make_channel(CHANNEL_ID)
    message = make_message(channel=channel, author_id=1, content="VETO P-bad-id")
    await bot.on_message(message)
    _, kwargs = message.reply.call_args
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


# -- rate limiting -----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_drops_excess_commands_with_one_rejection(bot: PortfolioBot, clock: FixedClock) -> None:
    channel = make_channel(CHANNEL_ID)
    for i in range(10):
        message = make_message(channel=channel, author_id=1, message_id=i, content="STATUS")
        await bot.on_message(message)
    eleventh = make_message(channel=channel, author_id=1, message_id=11, content="STATUS")
    await bot.on_message(eleventh)
    args, _ = eleventh.reply.call_args
    assert args[0].startswith("❌")
    assert "rate limited" in args[0].lower()


@pytest.mark.asyncio
async def test_rate_limit_is_per_sender(bot: PortfolioBot) -> None:
    channel = make_channel(CHANNEL_ID)
    for i in range(10):
        await bot.on_message(make_message(channel=channel, author_id=1, message_id=i))
    other = make_message(channel=channel, author_id=2, message_id=99)
    await bot.on_message(other)
    args, _ = other.reply.call_args
    assert args[0].startswith("✅")


@pytest.mark.asyncio
async def test_rate_limit_window_resets(bot: PortfolioBot, clock: FixedClock) -> None:
    channel = make_channel(CHANNEL_ID)
    for i in range(10):
        await bot.on_message(make_message(channel=channel, author_id=1, message_id=i))
    clock.advance(minutes=2)
    fresh = make_message(channel=channel, author_id=1, message_id=200)
    await bot.on_message(fresh)
    args, _ = fresh.reply.call_args
    assert args[0].startswith("✅")


# -- rejected control message alert rate limiting -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_message_enqueues_one_alert_per_window(bot: PortfolioBot, store: FileStore) -> None:
    channel = make_channel(CHANNEL_ID)
    stranger1 = make_message(channel=channel, author_id=99, message_id=1, content="STATUS")
    stranger2 = make_message(channel=channel, author_id=99, message_id=2, content="PAUSE")
    await bot.on_message(stranger1)
    await bot.on_message(stranger2)
    pending = list((store.root / "alerts" / "pending").glob("*.json"))
    assert len(pending) == 1


# -- cursor -----------------------------------------------------------------------------------------------------


def test_cursor_round_trip(bot: PortfolioBot) -> None:
    assert bot._read_cursor() == {}
    bot._write_cursor("555", 42)
    assert bot._read_cursor() == {"555": 42}
    bot._write_cursor("dm:1", 7)
    assert bot._read_cursor() == {"555": 42, "dm:1": 7}


@pytest.mark.asyncio
async def test_backfill_history_processes_messages_and_advances_cursor(bot: PortfolioBot) -> None:
    channel = make_channel(CHANNEL_ID)
    messages = [make_message(channel=channel, author_id=1, message_id=i, content="STATUS") for i in (10, 11)]

    async def history(**_kwargs: object) -> AsyncIterator[MagicMock]:
        for m in messages:
            yield m

    channel.history = history
    await bot._backfill_history(channel, str(CHANNEL_ID), {})
    for m in messages:
        m.reply.assert_awaited_once()
    assert bot._read_cursor()[str(CHANNEL_ID)] == 11


# -- outbox and heartbeat loops (one-shot, not the infinite loop) --------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_once_sends_pending_alerts(bot: PortfolioBot, store: FileStore) -> None:
    from stockmanager.notify.outbox import Alert, ChannelState

    alert = Alert(
        kind="system_ready",
        severity="info",
        title="System ready",
        body="The system is ready.",
        fields={},
        commands=[],
        needs_action=False,
        dedupe_key="system_ready",
        binds={},
        channels={"mainchat": ChannelState()},
    )
    enqueue(alert, store)
    await bot._deliver_once()
    assert not list((store.root / "alerts" / "pending").glob("*.json"))
    assert len(list((store.root / "alerts" / "sent").glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_write_heartbeat_writes_timestamp(bot: PortfolioBot, clock: FixedClock) -> None:
    await bot._write_heartbeat()
    payload = json.loads(bot._heartbeat_path.read_text())
    assert payload["at"] == clock.now.isoformat()
