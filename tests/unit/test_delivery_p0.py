"""v2 P0 delivery-layer gates: priority lanes + TTL, dedupe ledger, schema tolerance, thread filter, process split."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from stockmanager.bot.bot import PortfolioBot
from stockmanager.commands import CommandContext, Sender, run_command
from stockmanager.config import Config, DiscordConfig
from stockmanager.domain.docs import PortfolioDoc, ProposalDoc
from stockmanager.notify.deliver import (
    ALERT_TTL_HOURS,
    Permanent,
    Receipt,
    Rendered,
    Transient,
    deliver_pending,
)
from stockmanager.notify.outbox import Alert, ChannelState, enqueue
from stockmanager.notify.templates import bot_offline, veto_window_open
from stockmanager.sim.channels import RecordingChannel
from stockmanager.sim.faults import sqlite_down
from stockmanager.store import FileStore
from tests.support.books import FakePidChecker, FixedClock
from tests.support.proposals import PT, make_proposal
from tests.unit.test_bot import FakeCalendar, FakePriceProvider, make_channel, make_message

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=PT)
PID = "P-2026-10-12-01"
CHANNEL_ID = 555


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def store(tmp_path: Path, clock: FixedClock) -> FileStore:
    s = FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)
    s.save_portfolio(PortfolioDoc(start_cash=Decimal(10000), cash=Decimal(10000), status="LIVE"))
    return s


class Scripted:
    """A channel that records sends, and raises ``stall`` for alerts whose title is in ``stall_titles``."""

    def __init__(self, name: Literal["discord", "mainchat"], stall: Exception, stall_titles: set[str]) -> None:
        self.name = name
        self.sent: list[str] = []
        self._stall = stall
        self._stall_titles = stall_titles

    def send(self, rendered: Rendered, *, nonce: str) -> Receipt:
        if any(t in rendered.text for t in self._stall_titles):
            raise self._stall
        self.sent.append(rendered.text)
        return Receipt(self.name, nonce, NOW)


def narrative(title: str, key: str) -> Alert:
    return Alert("summary", "info", title, "body", {}, [], False, key, {}, channels={"discord": ChannelState()})


def ids(store: FileStore, box: str) -> list[str]:
    return sorted(p.stem for p in (store.root / "alerts" / box).glob("*.json"))


# -- 1. priority lanes + TTL ---------------------------------------------------------------------------------------


def test_priority_lane_and_ttl(store: FileStore, clock: FixedClock) -> None:
    # An older narrative alert is rate-limited; the veto alert queued after it must still go first and go out.
    enqueue(narrative("old narrative", "n1"), store)
    clock.advance(seconds=1)
    veto = veto_window_open(PID, 4)
    veto.channels = {"discord": ChannelState()}
    enqueue(veto, store)
    clock.advance(seconds=1)
    enqueue(narrative("new narrative", "n2"), store)
    chan = Scripted("discord", Transient("429", retry_after=60), {"old narrative", "new narrative"})
    delivered = deliver_pending(store, [chan], clock)
    assert chan.sent == [veto.title]
    assert delivered == [veto.alert_id]
    assert len(ids(store, "pending")) == 2  # both narratives wait, in order, for the stall to clear

    # TTL: a mainchat alert that failed Permanent and is older than the TTL is dead-lettered and frees the budget.
    stuck = narrative("stuck", "s1")
    stuck.channels = {"mainchat": ChannelState()}
    enqueue(stuck, store)
    clock.advance(hours=1)
    mainchat = Scripted("mainchat", Permanent("401"), {"stuck"})
    deliver_pending(store, [mainchat], clock)  # first Permanent failure, inside the TTL: still pending
    assert stuck.alert_id in ids(store, "pending")
    clock.advance(hours=ALERT_TTL_HOURS)
    deliver_pending(store, [mainchat], clock)
    assert stuck.alert_id in ids(store, "dead")
    assert stuck.alert_id not in ids(store, "pending")


def test_needs_action_alerts_share_the_priority_lane(store: FileStore, clock: FixedClock) -> None:
    enqueue(narrative("fyi", "a"), store)
    clock.advance(seconds=1)
    urgent = Alert("recon", "alert", "act now", "b", {}, ["STATUS"], True, "b", {}, channels={"discord": ChannelState()})
    enqueue(urgent, store)
    chan = RecordingChannel("discord", clock)
    deliver_pending(store, [chan], clock, budget=1)
    assert [n for n, _ in chan.sent] == [urgent.alert_id]


def test_ttl_dead_letters_unwired_channel_but_not_transient_retries(store: FileStore, clock: FixedClock) -> None:
    both = narrative("both", "k1")
    both.channels = {"discord": ChannelState(), "mainchat": ChannelState()}
    enqueue(both, store)
    retrying = narrative("retrying", "k2")
    enqueue(retrying, store)
    clock.advance(hours=ALERT_TTL_HOURS + 1)
    flaky = Scripted("discord", Transient("503"), {"retrying"})
    deliver_pending(store, [flaky], clock)  # mainchat has no channel wired at all
    assert ids(store, "dead") == [both.alert_id]
    assert retrying.alert_id in ids(store, "pending")  # Transient failure past the TTL is not a dead letter


# -- 2. dedupe ledger ----------------------------------------------------------------------------------------------


def test_double_enqueue_same_dedupe_key_creates_one_pending_file(store: FileStore, clock: FixedClock) -> None:
    assert enqueue(bot_offline(), store) is True
    assert enqueue(bot_offline(), store) is False
    assert len(ids(store, "pending")) == 1
    assert store.dedupe_keys() == {"bot_offline"}


def test_dedupe_survives_sqlite_down_and_lost_pending_file(store: FileStore, clock: FixedClock) -> None:
    assert enqueue(bot_offline(), store) is True
    for path in (store.root / "alerts" / "pending").glob("*.json"):
        path.unlink()  # the pending scan cannot see it any more; the file ledger still does
    with sqlite_down():
        assert enqueue(bot_offline(), store) is False
        assert enqueue(narrative("fresh", "other"), store) is True
    assert len(ids(store, "pending")) == 1


def test_dedupe_key_is_released_on_dead_letter(store: FileStore, clock: FixedClock) -> None:
    alert = narrative("x", "kx")
    alert.channels = {"mainchat": ChannelState()}
    enqueue(alert, store)
    clock.advance(hours=ALERT_TTL_HOURS + 1)
    deliver_pending(store, [Scripted("discord", Transient("x"), set())], clock)
    assert alert.alert_id in ids(store, "dead")
    assert "kx" not in store.dedupe_keys()


# -- 3. Alert.from_json tolerance ----------------------------------------------------------------------------------


def test_old_stored_alert_with_new_fields_replays(store: FileStore, clock: FixedClock) -> None:
    alert = narrative("v1", "kv")
    enqueue(alert, store)
    path = store.root / "alerts" / "pending" / f"{alert.alert_id}.json"
    data = json.loads(path.read_text())
    data["schema_version"] = 2
    data["lane"] = "priority"
    data["channels"]["discord"]["future_field"] = {"x": 1}
    path.write_text(json.dumps(data))
    loaded = Alert.from_json(path.read_text())
    assert loaded.alert_id == alert.alert_id and loaded.channels["discord"].state == "pending"
    chan = RecordingChannel("discord", clock)
    assert deliver_pending(store, [chan], clock) == [alert.alert_id]  # the replay path does not crash


def test_alert_json_missing_optional_fields_replays() -> None:
    minimal = {"alert_id": "a", "kind": "k", "severity": "info", "title": "t", "body": "b"}
    loaded = Alert.from_json(json.dumps(minimal))
    assert loaded.channels == {} and loaded.dedupe_key is None and loaded.occurred_at is None


# -- 4. thread filter ----------------------------------------------------------------------------------------------


def command_ctx(store: FileStore, clock: FixedClock) -> CommandContext:
    cfg = Config(discord=DiscordConfig(allowed_user_ids=["1"], channel_id=str(CHANNEL_ID)))
    return CommandContext(store=store, cfg=cfg, clock=clock, calendar=FakeCalendar(), price_provider=FakePriceProvider())


def make_thread(thread_id: int, parent_id: int) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    return thread


def test_proposal_delivery_and_thread_creation_in_the_same_tick(store: FileStore, clock: FixedClock) -> None:
    proposal = make_proposal(state="PROPOSED", created_at=NOW, proposal_id=PID)
    store.save_proposal(PID, proposal)
    alert = veto_window_open(PID, 4)
    enqueue(alert, store)
    discord_chan = RecordingChannel("discord", clock, threads=True)
    mainchat = RecordingChannel("mainchat", clock)
    deliver_pending(store, [discord_chan, mainchat], clock)
    doc = ProposalDoc.model_validate_json(store.proposal_path(PID).read_text())
    assert [r.channel for r in doc.delivery.receipts] == ["discord"]
    assert doc.delivery.thread_id == f"thread-{alert.alert_id}" == discord_chan.thread_ids[0]


@pytest.mark.asyncio
async def test_thread_reply_routes_to_handler(store: FileStore, clock: FixedClock) -> None:
    bot = PortfolioBot(store, CHANNEL_ID, command_ctx(store, clock), [RecordingChannel("discord", clock)], clock=clock)
    thread_msg = make_message(channel=make_thread(900, CHANNEL_ID), content="STATUS")
    await bot.on_message(thread_msg)
    thread_msg.reply.assert_awaited_once()
    assert thread_msg.reply.call_args.args[0].startswith("✅")
    foreign = make_message(channel=make_thread(901, 999), content="STATUS")
    await bot.on_message(foreign)
    foreign.reply.assert_not_awaited()
    plain = make_message(channel=make_channel(CHANNEL_ID), content="STATUS")
    await bot.on_message(plain)
    plain.reply.assert_awaited_once()


# -- 5. bot/scheduler process split --------------------------------------------------------------------------------


def test_command_runs_with_the_bot_absent_and_bot_restarts_independently(store: FileStore, clock: FixedClock) -> None:
    ctx = command_ctx(store, clock)
    proposal = make_proposal(
        state="VETO_WINDOW", created_at=NOW, proposal_id=PID, approval_mode="explicit", execute_at=NOW + timedelta(hours=24)
    )
    store.save_proposal(PID, proposal)
    # "Scheduler process": no PortfolioBot exists anywhere; the entry takes only the context.
    reply = run_command(f"APPROVE {PID}", Sender("cli", None, None, NOW), ctx)
    assert reply.startswith("✅"), reply
    approved = ProposalDoc.model_validate_json(store.proposal_path(PID).read_text())
    assert approved.state == "APPROVED"  # the state the execution engine fills from

    # "Bot process" starts afterwards on the same store, holding none of the scheduler's state.
    bot = PortfolioBot(store, CHANNEL_ID, command_ctx(store, clock), [RecordingChannel("discord", clock)], clock=clock)
    assert bot._read_cursor() == {}
    assert run_command("STATUS", Sender("cli", None, None, NOW), bot._ctx).startswith("✅")
    del bot  # bot dies; the scheduler-side entry keeps working
    assert run_command("STATUS", Sender("cli", None, None, NOW), ctx).startswith("✅")
    _ = AsyncMock  # (async doubles are only needed by the thread-routing test)
