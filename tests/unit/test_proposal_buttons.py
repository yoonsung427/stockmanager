"""v2 H4: VETO / APPROVE buttons on proposal embeds resolve through the typed-command handler (TS §2, §3 H4)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from stockmanager.bot.bot import PortfolioBot
from stockmanager.commands import CommandContext, Sender, run_command
from stockmanager.config import Config, DiscordConfig
from stockmanager.domain.docs import ProposalDoc
from stockmanager.notify import deliver_pending, enqueue, prepare, veto_window_open
from stockmanager.notify.components import click_command, custom_id, parse_custom_id, proposal_components
from stockmanager.sim.channels import RecordingChannel
from stockmanager.sim.clock import FakeClock
from stockmanager.store import FileStore
from tests.support.assertions import alerts
from tests.support.books import FakePidChecker
from tests.support.proposals import make_proposal
from tests.unit.test_bot import CHANNEL_ID, FakeCalendar, FakePriceProvider

PT = ZoneInfo("America/Los_Angeles")
PID = "P-2026-10-12-01"
T0 = datetime(2026, 10, 12, 7, 0, tzinfo=PT)
CLICK_AT = datetime(2026, 10, 12, 9, 0, tzinfo=PT)  # inside the window; execute_at is 11:00
REJECTION = "❌ Sender not authorized"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> FileStore:
    return FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)


@pytest.fixture
def ctx(store: FileStore, clock: FakeClock) -> CommandContext:
    cfg = Config(discord=DiscordConfig(allowed_user_ids=["1", "2"], channel_id=str(CHANNEL_ID)))
    return CommandContext(store=store, cfg=cfg, clock=clock, calendar=FakeCalendar(), price_provider=FakePriceProvider())


def make_bot(store: FileStore, ctx: CommandContext, clock: FakeClock) -> PortfolioBot:
    return PortfolioBot(store, CHANNEL_ID, ctx, [RecordingChannel("mainchat", clock)], clock=clock)


def save(store: FileStore, **kwargs: Any) -> ProposalDoc:
    doc = make_proposal(PID, created_at=T0, **kwargs)
    store.save_proposal(PID, doc)
    return doc


def load(store: FileStore) -> ProposalDoc:
    return ProposalDoc.model_validate_json(store.proposal_path(PID).read_text(encoding="utf-8"))


def deliver_proposal_alert(store: FileStore, clock: FakeClock) -> RecordingChannel:
    channel = RecordingChannel("discord", clock)
    enqueue(veto_window_open(PID, 4), store)
    deliver_pending(store, [channel], clock)
    return channel


# -- the components ------------------------------------------------------------------------------------------------


def test_proposal_embed_exposes_veto_and_approve_components(store: FileStore, clock: FakeClock) -> None:
    save(store)
    channel = deliver_proposal_alert(store, clock)
    [(_, rendered)] = channel.sent
    assert rendered.components is not None
    buttons = rendered.components[0]["components"]
    assert [b["label"] for b in buttons] == ["Veto", "Approve"]
    assert [b["custom_id"] for b in buttons] == [custom_id("VETO", PID), custom_id("APPROVE", PID)]
    assert rendered.embed is not None and any(f["name"] == "Commands" for f in rendered.embed["fields"])  # typed fallback


def test_only_proposal_alerts_on_discord_carry_buttons(store: FileStore, clock: FakeClock) -> None:
    from stockmanager.notify import system_ready

    save(store)
    discord_channel, mainchat = RecordingChannel("discord", clock), RecordingChannel("mainchat", clock)
    enqueue(veto_window_open(PID, 4), store)
    enqueue(system_ready(), store)
    deliver_pending(store, [discord_channel, mainchat], clock)
    by_title = {r.text: r for _, r in discord_channel.sent}
    assert by_title["System ready"].components is None
    assert by_title[f"Veto window open: {PID}"].components is not None
    assert all(r.components is None for _, r in mainchat.sent)


def test_custom_id_codec_round_trips_and_rejects_foreign_ids() -> None:
    assert parse_custom_id(custom_id("VETO", PID)) == ("VETO", PID)
    assert parse_custom_id(custom_id("APPROVE", PID)) == ("APPROVE", PID)
    for bad in ("", "sm:PAUSE:" + PID, "sm:VETO:P-1", "x:VETO:" + PID, f"sm:VETO:{PID} extra"):
        assert parse_custom_id(bad) is None
    assert proposal_components("not-an-id") == []
    assert click_command("VETO", PID, "  too risky ") == f"VETO {PID}   too risky "  # the parser sanitises it
    assert click_command("APPROVE", PID, "ignored") == f"APPROVE {PID}"


# -- same handler, same audit ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_click_routes_to_same_handler_as_typed_command(
    store: FileStore, ctx: CommandContext, clock: FakeClock, tmp_path: Path
) -> None:
    save(store)
    twin_store = FileStore(tmp_path / "twin", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)
    twin_store.save_proposal(PID, make_proposal(PID, created_at=T0))
    twin_ctx = CommandContext(
        store=twin_store, cfg=ctx.cfg, clock=clock, calendar=FakeCalendar(), price_provider=FakePriceProvider()
    )

    click_reply = await make_bot(store, ctx, clock).handle_component(custom_id("VETO", PID), "1", "i-1", CLICK_AT)
    typed_reply = run_command(f"VETO {PID}", Sender("discord", "1", "m-1", CLICK_AT), twin_ctx)

    assert click_reply == typed_reply == f"✅ Vetoed {PID}."
    clicked, typed = load(store), ProposalDoc.model_validate_json(twin_store.proposal_path(PID).read_text())
    assert clicked.state == typed.state == "VETOED"
    assert clicked.transitions[-1] == typed.transitions[-1]  # same actor, time, cause, hash


@pytest.mark.asyncio
async def test_click_audit_row_records_sender_timestamp_and_optional_reason(
    store: FileStore, ctx: CommandContext, clock: FakeClock
) -> None:
    save(store)
    await make_bot(store, ctx, clock).handle_component(custom_id("VETO", PID), "2", "i-2", CLICK_AT, "  too   risky ")
    row = load(store).transitions[-1]
    assert (row.to, row.actor, row.at, row.cause) == ("VETOED", "discord:2", CLICK_AT, "too risky")


@pytest.mark.asyncio
async def test_click_without_reason_leaves_the_default_cause(
    store: FileStore, ctx: CommandContext, clock: FakeClock
) -> None:
    save(store)
    await make_bot(store, ctx, clock).handle_component(custom_id("VETO", PID), "1", "i-3", CLICK_AT)
    row = load(store).transitions[-1]
    assert (row.actor, row.at, row.cause) == ("discord:1", CLICK_AT, "vetoed")


@pytest.mark.asyncio
async def test_approve_click_approves_an_explicit_proposal(
    store: FileStore, ctx: CommandContext, clock: FakeClock
) -> None:
    save(store, approval_mode="explicit")
    reply = await make_bot(store, ctx, clock).handle_component(custom_id("APPROVE", PID), "1", "i-4", CLICK_AT)
    assert reply == f"✅ Approved {PID}."
    assert (load(store).state, load(store).transitions[-1].actor) == ("APPROVED", "discord:1")


@pytest.mark.asyncio
async def test_clicker_not_in_allow_list_rejected(store: FileStore, ctx: CommandContext, clock: FakeClock) -> None:
    save(store)
    before = store.proposal_path(PID).read_text()
    bot = make_bot(store, ctx, clock)
    click_reply = await bot.handle_component(custom_id("VETO", PID), "99", "i-5", CLICK_AT)
    typed_reply = run_command(f"VETO {PID}", Sender("discord", "99", "m-5", CLICK_AT), ctx)
    assert click_reply == typed_reply == REJECTION  # through authorize(), identical reason text
    assert store.proposal_path(PID).read_text() == before  # nothing recorded, no transition
    assert len(alerts(store.root, "rejected_control_message")) == 1  # same rejection alert as a typed one
    approve = await bot.handle_component(custom_id("APPROVE", PID), "99", "i-6", CLICK_AT)
    assert approve == REJECTION


@pytest.mark.asyncio
async def test_click_with_empty_allow_list_is_disabled_like_typed(
    store: FileStore, clock: FakeClock
) -> None:
    save(store)
    cfg = Config(discord=DiscordConfig(allowed_user_ids=[], channel_id=str(CHANNEL_ID)))
    ctx = CommandContext(store=store, cfg=cfg, clock=clock, calendar=FakeCalendar(), price_provider=FakePriceProvider())
    click = await make_bot(store, ctx, clock).handle_component(custom_id("VETO", PID), "1", "i-7", CLICK_AT)
    assert click == run_command(f"VETO {PID}", Sender("discord", "1", "m", CLICK_AT), ctx)
    assert click is not None and "disabled" in click


# -- bot down -------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_button_click_while_bot_down(store: FileStore, ctx: CommandContext, clock: FakeClock) -> None:
    """Ruling: persistent buttons, no queue. A click with no bot running changes nothing and is not replayed (Discord
    shows its own failure notice); the same embed's buttons work after a restart, and every click gets a reply."""
    save(store)
    channel = deliver_proposal_alert(store, clock)  # delivery is REST: it needs no bot
    [(_, rendered)] = channel.sent
    assert rendered.components is not None
    veto_id = rendered.components[0]["components"][0]["custom_id"]

    # Downtime: no PortfolioBot exists, so the click reaches nothing. It must leave no half-state behind ...
    transitions_before = len(load(store).transitions)
    clock.advance(minutes=30)
    assert load(store).state != "VETOED" and len(load(store).transitions) == transitions_before
    # ... and the persistence is structural: the components carry no timeout or view state, only a decodable id.
    assert "timeout" not in rendered.components[0] and parse_custom_id(veto_id) == ("VETO", PID)
    assert rendered.embed is not None and any(f["name"] == "Commands" for f in rendered.embed["fields"])

    # Restart: a fresh process resolves a click on the old embed. ``at`` stays the click's own time (TS D11).
    restarted = make_bot(store, ctx, clock)
    reply = await restarted.handle_component(veto_id, "1", "i-8", CLICK_AT)
    assert reply == f"✅ Vetoed {PID}."
    row = load(store).transitions[-1]
    assert (row.actor, row.at) == ("discord:1", CLICK_AT)

    # A late or repeated click is answered, never swallowed.
    again = await restarted.handle_component(veto_id, "1", "i-9", CLICK_AT)
    assert again is not None and again.startswith("❌ Cannot VETO")


@pytest.mark.asyncio
async def test_late_click_after_execution_time_is_answered_not_dropped(
    store: FileStore, ctx: CommandContext, clock: FakeClock
) -> None:
    save(store, approval_mode="explicit")
    late = load(store).execute_at + timedelta(minutes=1)
    reply = await make_bot(store, ctx, clock).handle_component(custom_id("APPROVE", PID), "1", "i-10", late)
    assert reply is not None and reply.startswith("❌") and "execution time has passed" in reply
    assert load(store).state == "VETO_WINDOW"


# -- the discord.py seam --------------------------------------------------------------------------------------------


def make_interaction(custom: str, user_id: int = 1, kind: discord.InteractionType = discord.InteractionType.component) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.type = kind
    interaction.data = {"custom_id": custom}
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.id = 777
    interaction.created_at = CLICK_AT
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_on_interaction_replies_ephemerally_with_the_handler_text(
    store: FileStore, ctx: CommandContext, clock: FakeClock
) -> None:
    save(store)
    bot = make_bot(store, ctx, clock)
    rejected = make_interaction(custom_id("VETO", PID), user_id=99)
    await bot.on_interaction(rejected)
    rejected.response.defer.assert_awaited_once_with(ephemeral=True)
    rejected.followup.send.assert_awaited_once()
    assert rejected.followup.send.call_args.args[0] == REJECTION
    assert rejected.followup.send.call_args.kwargs["ephemeral"] is True
    ok = make_interaction(custom_id("VETO", PID), user_id=1)
    await bot.on_interaction(ok)
    assert ok.followup.send.call_args.args[0] == f"✅ Vetoed {PID}."


@pytest.mark.asyncio
async def test_on_interaction_ignores_foreign_components(store: FileStore, ctx: CommandContext, clock: FakeClock) -> None:
    bot = make_bot(store, ctx, clock)
    foreign = make_interaction("other:thing")
    await bot.on_interaction(foreign)
    foreign.response.defer.assert_not_awaited()
    slash = make_interaction(custom_id("VETO", PID), kind=discord.InteractionType.application_command)
    await bot.on_interaction(slash)
    slash.response.defer.assert_not_awaited()


def test_rest_channel_posts_components_with_the_embed() -> None:
    import json

    import httpx

    from stockmanager.adapters.discord_rest import DiscordRESTChannel
    from stockmanager.notify import Alert, render

    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "42"})

    channel = DiscordRESTChannel("tok", CHANNEL_ID, client=httpx.Client(transport=httpx.MockTransport(handler)))
    alert: Alert = prepare(veto_window_open(PID, 4), T0)
    channel.send(render(alert, "discord", T0), nonce="n1")
    assert seen[0]["components"][0]["components"][1]["custom_id"] == custom_id("APPROVE", PID)
    assert seen[0]["embeds"][0]["title"].startswith("Veto window open")
