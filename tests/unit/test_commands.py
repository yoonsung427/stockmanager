"""Commands: parser, authorization, and the handlers (AS §1.7)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pytest

from stockmanager.commands.auth import AuthorizationResult, Sender, authorize
from stockmanager.commands.handlers import CommandContext, cmd_pause, cmd_resume, cmd_set, cmd_veto, handle
from stockmanager.commands.parser import ParsedCommand, parse_command
from stockmanager.config import Config, DiscordConfig
from stockmanager.domain.docs import PriceQuote
from stockmanager.store import FileStore
from tests.support.assertions import alerts
from tests.support.books import FakePidChecker, FixedClock, make_portfolio
from tests.support.proposals import make_proposal

PT = ZoneInfo("America/Los_Angeles")


class FakeCalendar:
    def next_session_open_at_or_after(self, instant: datetime) -> datetime:
        return instant


class FakePriceProvider:
    name: Literal["yahoo", "finnhub"] = "yahoo"

    def get_price(self, ticker: str, session_date: date, kind: Literal["open", "close"]) -> PriceQuote:
        return PriceQuote(
            ticker=ticker,
            session_date=session_date,
            kind=kind,
            price=Decimal(150),
            source="yahoo",
            fetched_at=datetime.now(PT),
        )

    def prime(self, tickers: Sequence[str], session_date: date, kind: Literal["open", "close"]) -> dict[str, Exception]:
        return {}


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def store(tmp_path: Path, clock: FixedClock) -> FileStore:
    return FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)


@pytest.fixture
def ctx(store: FileStore, clock: FixedClock) -> CommandContext:
    return CommandContext(
        store=store, cfg=Config(), clock=clock, calendar=FakeCalendar(), price_provider=FakePriceProvider()
    )


def cli(sent_at: datetime) -> Sender:
    return Sender("cli", None, None, sent_at)


def discord_sender(user_id: str, sent_at: datetime) -> Sender:
    return Sender("discord", user_id, "m1", sent_at)


# -- parser ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("VETO P-2026-10-12-01 too risky", ParsedCommand("VETO", "P-2026-10-12-01", "too risky")),
        ("veto P-2026-10-12-01", ParsedCommand("VETO", "P-2026-10-12-01", None)),
        ("APPROVE P-2026-10-12-01", ParsedCommand("APPROVE", "P-2026-10-12-01", None)),
        ("PAUSE", ParsedCommand("PAUSE", None, None)),
        ("RESUME", ParsedCommand("RESUME", None, None)),
        ("STATUS", ParsedCommand("STATUS", None, None)),
        ("CLOSE", ParsedCommand("CLOSE", None, None)),
        ("CLOSE CONFIRM", ParsedCommand("CLOSE_CONFIRM", None, None)),
        ("RECONCILE", ParsedCommand("RECONCILE", "readonly", None)),
        ("RECONCILE rebuild", ParsedCommand("RECONCILE", "rebuild", None)),
        ("SET max_position_pct 25", ParsedCommand("SET", "max_position_pct", "25")),
        ("RECONFIRM P-2026-10-12-01", ParsedCommand("RECONFIRM", "P-2026-10-12-01", None)),
    ],
)
def test_parse_valid_commands(text: str, expected: ParsedCommand) -> None:
    assert parse_command(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "please VETO P-2026-10-12-01",  # verb must start the message (TS CMD-X02)
        "BOGUS",
        "hello there",
    ],
)
def test_parse_invalid_commands_return_none(text: str) -> None:
    assert parse_command(text) is None


def test_veto_reason_is_sanitised_and_clipped() -> None:
    parsed = parse_command("VETO P-2026-10-12-01 " + "x" * 600 + "\x00\x01")
    assert parsed is not None
    assert parsed.reason is not None
    assert len(parsed.reason) == 500
    assert "\x00" not in parsed.reason


# -- auth -----------------------------------------------------------------------------------------------------


def test_cli_is_always_authorized() -> None:
    cfg = Config()
    result = authorize(cli(datetime.now(PT)), ParsedCommand("STATUS", None, None), cfg)
    assert result == AuthorizationResult(True)


def test_discord_allowed_user_is_authorized() -> None:
    cfg = Config(discord=DiscordConfig(allowed_user_ids=["42"]))
    result = authorize(discord_sender("42", datetime.now(PT)), ParsedCommand("STATUS", None, None), cfg)
    assert result.ok


def test_discord_user_not_on_allowlist_is_rejected() -> None:
    cfg = Config(discord=DiscordConfig(allowed_user_ids=["42"]))
    result = authorize(discord_sender("99", datetime.now(PT)), ParsedCommand("STATUS", None, None), cfg)
    assert not result.ok


def test_discord_empty_allowlist_disables_discord_control() -> None:
    cfg = Config()
    result = authorize(discord_sender("42", datetime.now(PT)), ParsedCommand("STATUS", None, None), cfg)
    assert not result.ok
    assert result.reason is not None and "disabled" in result.reason


# -- VETO -------------------------------------------------------------------------------------------------------


def test_veto_valid_proposal(ctx: CommandContext, store: FileStore, clock: FixedClock) -> None:
    doc = make_proposal(state="VETO_WINDOW")
    store.save_proposal(doc.proposal_id, doc)
    reply = cmd_veto(ctx, doc.proposal_id, "too risky", cli(clock.now))
    assert reply.startswith("✅")
    from stockmanager.domain.docs import ProposalDoc

    reloaded_doc = ProposalDoc.model_validate_json(store.proposal_path(doc.proposal_id).read_text())
    assert reloaded_doc.state == "VETOED"
    assert reloaded_doc.transitions[-1].cause == "too risky"
    assert reloaded_doc.transitions[-1].at == clock.now


def test_veto_already_executed_is_rejected(ctx: CommandContext, store: FileStore, clock: FixedClock) -> None:
    doc = make_proposal(state="EXECUTED")
    store.save_proposal(doc.proposal_id, doc)
    reply = cmd_veto(ctx, doc.proposal_id, None, cli(clock.now))
    assert reply.startswith("❌")


def test_veto_bad_id_is_rejected(ctx: CommandContext, clock: FixedClock) -> None:
    assert cmd_veto(ctx, "not-an-id", None, cli(clock.now)).startswith("❌")
    assert cmd_veto(ctx, None, None, cli(clock.now)).startswith("❌")


def test_every_command_gets_a_reply(ctx: CommandContext, clock: FixedClock) -> None:
    for text in ["STATUS", "PAUSE", "RESUME", "BOGUS", "VETO P-0000-00-00-00", "SET foo bar"]:
        reply = handle(text, cli(clock.now), ctx)
        assert reply.startswith(("✅", "❌"))


# -- PAUSE / RESUME -----------------------------------------------------------------------------------------------


def test_pause_cancels_non_terminal_proposals(ctx: CommandContext, store: FileStore, clock: FixedClock) -> None:
    store.save_portfolio(make_portfolio(status="LIVE"))
    p1 = make_proposal("P-2026-10-12-01", state="PROPOSED")
    p2 = make_proposal("P-2026-10-12-02", state="VETO_WINDOW")
    done = make_proposal("P-2026-10-12-03", state="EXECUTED")
    for p in (p1, p2, done):
        store.save_proposal(p.proposal_id, p)
    reply = cmd_pause(ctx, cli(clock.now))
    assert reply.startswith("✅")
    assert store.load_portfolio().status == "PAUSED"
    from stockmanager.domain.docs import ProposalDoc

    assert (
        ProposalDoc.model_validate_json(store.proposal_path("P-2026-10-12-01").read_text()).state == "CANCELLED_PAUSED"
    )
    assert (
        ProposalDoc.model_validate_json(store.proposal_path("P-2026-10-12-02").read_text()).state == "CANCELLED_PAUSED"
    )
    assert ProposalDoc.model_validate_json(store.proposal_path("P-2026-10-12-03").read_text()).state == "EXECUTED"
    pending = list((store.root / "alerts" / "pending").glob("*.json"))
    assert len(pending) == 1


def test_pause_requires_live(ctx: CommandContext, store: FileStore, clock: FixedClock) -> None:
    store.save_portfolio(make_portfolio(status="PAUSED"))
    assert cmd_pause(ctx, cli(clock.now)).startswith("❌")


def test_resume_only_restores_live(ctx: CommandContext, store: FileStore, clock: FixedClock) -> None:
    store.save_portfolio(make_portfolio(status="PAUSED"))
    reply = cmd_resume(ctx, cli(clock.now))
    assert reply.startswith("✅")
    assert store.load_portfolio().status == "LIVE"


def test_resume_rejected_when_not_paused(ctx: CommandContext, store: FileStore, clock: FixedClock) -> None:
    store.save_portfolio(make_portfolio(status="LIVE"))
    assert cmd_resume(ctx, cli(clock.now)).startswith("❌")


# -- SET ------------------------------------------------------------------------------------------------------


def test_set_allowed_key(ctx: CommandContext, store: FileStore, clock: FixedClock) -> None:
    store.save_portfolio(make_portfolio(status="LIVE"))
    reply = cmd_set(ctx, "max_position_pct", "25", cli(clock.now))
    assert reply.startswith("✅")
    assert ctx.cfg.max_position_pct == Decimal(25)


@pytest.mark.parametrize(
    "key", ["ops.exec_delay_minutes", "llm.reasoner_model", "news.max_items_per_query", "discord.channel_id"]
)
def test_set_blocked_namespaces_are_rejected(
    ctx: CommandContext, store: FileStore, clock: FixedClock, key: str
) -> None:
    store.save_portfolio(make_portfolio(status="LIVE"))
    assert cmd_set(ctx, key, "1", cli(clock.now)).startswith("❌")


def test_set_invalid_value_is_rejected(ctx: CommandContext, store: FileStore, clock: FixedClock) -> None:
    store.save_portfolio(make_portfolio(status="LIVE"))
    assert cmd_set(ctx, "max_position_pct", "999", cli(clock.now)).startswith("❌")


def test_set_unknown_key_is_rejected(ctx: CommandContext, store: FileStore, clock: FixedClock) -> None:
    store.save_portfolio(make_portfolio(status="LIVE"))
    assert cmd_set(ctx, "not_a_real_key", "1", cli(clock.now)).startswith("❌")


# -- CLOSE / CLOSE CONFIRM -----------------------------------------------------------------------------------------


def test_close_then_confirm_closes_with_a_final_report_and_sells_nothing(
    ctx: CommandContext, store: FileStore, clock: FixedClock
) -> None:
    """TS CMD-C02: the final valuation closes the book; positions are kept and ``trades[]`` is unchanged."""
    from tests.support.books import make_trade

    store.save_portfolio(make_portfolio([make_trade(1, shares="10", price="100")], status="LIVE"))
    held, trades = store.load_portfolio().positions, store.load_portfolio().trades
    sender = cli(clock.now)
    assert handle("CLOSE", sender, ctx).startswith("✅")
    clock.advance(minutes=2)
    reply = handle("CLOSE CONFIRM", cli(clock.now), ctx)
    assert reply.startswith("✅")
    doc = store.load_portfolio()
    assert doc.status == "CLOSED" and doc.final_report is not None
    assert doc.positions == held and doc.trades == trades
    (alert,) = alerts(store.root, "final_report")
    assert alert["fields"]["trade_summary"] == "1 trades: 1 buys, 0 sells" and alert["fields"]["best_worst"]


def test_close_confirm_without_close_is_rejected(ctx: CommandContext, store: FileStore, clock: FixedClock) -> None:
    store.save_portfolio(make_portfolio(status="LIVE"))
    assert handle("CLOSE CONFIRM", cli(clock.now), ctx).startswith("❌")


def test_close_confirm_from_different_authority_is_rejected(
    ctx: CommandContext, store: FileStore, clock: FixedClock
) -> None:
    store.save_portfolio(make_portfolio(status="LIVE"))
    handle("CLOSE", discord_sender("1", clock.now), ctx)
    # same command content from a stranger does not get authorized at all in handle(), so call the handler directly
    from stockmanager.commands.handlers import cmd_close_confirm

    assert cmd_close_confirm(ctx, discord_sender("2", clock.now)).startswith("❌")


def test_close_confirm_after_window_is_rejected(ctx: CommandContext, store: FileStore, clock: FixedClock) -> None:
    store.save_portfolio(make_portfolio(status="LIVE"))
    sender = cli(clock.now)
    assert handle("CLOSE", sender, ctx).startswith("✅")
    clock.advance(minutes=11)
    assert handle("CLOSE CONFIRM", cli(clock.now), ctx).startswith("❌")


# -- STATUS ----------------------------------------------------------------------------------------------------


def test_status_is_lock_free_and_read_only(ctx: CommandContext, store: FileStore, clock: FixedClock) -> None:
    store.save_portfolio(make_portfolio(status="LIVE"))
    held = store.lock.acquire("other", 1, 0)
    reply = handle("STATUS", cli(clock.now), ctx)
    assert reply.startswith("✅")
    assert store.lock.is_held()
    store.lock.release(held.token)


def test_status_with_no_portfolio(ctx: CommandContext, clock: FixedClock) -> None:
    assert handle("STATUS", cli(clock.now), ctx).startswith("✅")


# -- RECONCILE -------------------------------------------------------------------------------------------------


def test_reconcile_readonly_reports_clean_books(ctx: CommandContext, store: FileStore, clock: FixedClock) -> None:
    from stockmanager.store.reconcile import format_log_line
    from tests.support.books import make_trade

    trades = [make_trade(1)]
    store.save_portfolio(make_portfolio(trades, status="LIVE"))
    store.append_trades_log([format_log_line(t) for t in trades])
    assert handle("RECONCILE", cli(clock.now), ctx).startswith("✅")


def test_reconcile_rebuild_rewrites_the_log(ctx: CommandContext, store: FileStore, clock: FixedClock) -> None:
    from tests.support.books import make_trade

    trades = [make_trade(1)]
    store.save_portfolio(make_portfolio(trades, status="LIVE"))
    # trades.log is missing entirely: reconcile() auto-repairs a missing suffix, so corrupt it to force a real halt
    store.trades_log_path.parent.mkdir(parents=True, exist_ok=True)
    store.trades_log_path.write_text("not a valid log line\n")
    reply = handle("RECONCILE rebuild", cli(clock.now), ctx)
    assert reply.startswith("✅")
    assert store.trades_log_path.read_text().strip() != "not a valid log line"


# -- auth integration via handle() ----------------------------------------------------------------------------


def test_handle_rejects_unauthorized_discord_sender(ctx: CommandContext, clock: FixedClock) -> None:
    reply = handle("STATUS", discord_sender("1", clock.now), ctx)
    assert reply.startswith("❌")


def test_handle_too_long_message_is_rejected(ctx: CommandContext, clock: FixedClock) -> None:
    assert handle("STATUS " + "x" * 3000, cli(clock.now), ctx).startswith("❌")
