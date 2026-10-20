"""Close: final valuation -> proposals cancelled -> status CLOSED -> no further runs (TS E2E-07, CMD-C01..C10, D5, D19).

Both paths close the book the same way: ``CLOSE CONFIRM`` runs ``ExecutionEngine.run_final_valuation`` (positions are
not sold, ``final_report`` recorded, open proposals cancelled), and the tests below drive it directly and through the
command.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from stockmanager.commands.auth import Sender
from stockmanager.commands.handlers import CommandContext, handle
from stockmanager.config import Config
from stockmanager.config.model import DiscordConfig
from stockmanager.scheduler.tick import tick
from stockmanager.sim import StubReasoner
from tests.support.assertions import (
    alerts,
    assert_lockfree,
    assert_logsync,
    assert_nofill,
    assert_recon,
    books,
    proposal_state,
    workspace_hashes,
)
from tests.support.reasoner_output import pair_output
from tests.support.world import EVAL_SAT, EXEC, PAIR_ID, SINGLE_ID, World, pr_pair, pr_single

CLOSE_AT = datetime(2026, 10, 22, 14, 0, tzinfo=EXEC.tzinfo)  # a Thursday afternoon, before the open proposals fill


def owner(at: datetime) -> Sender:
    """The trusted CLI owner."""
    return Sender("cli", None, None, at)


def ctx(w: World, cfg: Config | None = None) -> CommandContext:
    """A command context over the world's current store."""
    return CommandContext(w.store, cfg or Config(), w.clock, w.calendar, w.yahoo)


@pytest.fixture
def live(world: World) -> World:
    """S-LIVE with an approved pair and a veto-window single open, the clock on Thursday 14:00."""
    world.seed()
    world.put(pr_pair())
    world.put(pr_single(state="VETO_WINDOW"))
    world.clock.set(CLOSE_AT)
    return world


# -- the spec path: run_final_valuation ----------------------------------------------------------------------------------------


def test_e2e07_final_valuation_closes_the_books_without_selling(live: World) -> None:
    """TS E2E-07 / CMD-C02 / CMD-C08 / D19: CLOSE then the final valuation. One source, ending value and ROI equal an
    independent calculation, positions kept, every open proposal CANCELLED_CLOSED, pending marker gone."""
    live.prices("yahoo", AAPL=Decimal(220))  # AAPL +20 on 100 shares: NAV 50,000 -> 52,000
    assert handle("CLOSE", owner(CLOSE_AT), ctx(live)).startswith("✅")
    marker = live.root / "state" / "pending_close.json"
    assert marker.exists() and live.store.load_portfolio().status == "LIVE"  # CMD-C01: asking changes nothing
    before = books(live.store)
    held = sorted(p.ticker for p in live.store.load_portfolio().positions)

    assert live.engine().run_final_valuation(benchmark="SPY").outcome == "completed"

    doc = live.store.load_portfolio()
    assert doc.status == "CLOSED" and doc.closed_at == CLOSE_AT
    assert doc.final_report is not None
    expected_nav = Decimal(17000) + 100 * Decimal(220) + 50 * Decimal(100) + 20 * Decimal(400)
    assert Decimal(doc.final_report["ending_value"]) == expected_nav == Decimal(52000)
    assert Decimal(doc.final_report["roi_pct"]) == (expected_nav - 50000) / 50000 * 100 == Decimal(4)
    assert doc.final_report["source"] == "yahoo"
    (alert,) = alerts(live.root, "final_report")
    assert alert["fields"]["benchmark_relative"].startswith("+4.0000% vs SPY +0.0000%")  # SPY flat, portfolio +4%
    assert alert["fields"]["best_worst"].startswith("Best: AAPL +") and "Worst: " in alert["fields"]["best_worst"]
    assert "trades:" in alert["fields"]["trade_summary"]
    assert books(live.store).trade_ids == before.trade_ids  # positions are not sold
    assert sorted(p.ticker for p in doc.positions) == held == ["AAPL", "JPM", "MSFT"]
    assert proposal_state(live.store, PAIR_ID) == proposal_state(live.store, SINGLE_ID) == "CANCELLED_CLOSED"
    assert not marker.exists()
    final_rows = [json.loads(line) for line in live.store.nav_history_path.read_text(encoding="utf-8").splitlines()]
    assert [(r["kind"], r["nav"]) for r in final_rows] == [("final", "52000.0000")]
    assert_recon(live.store)
    assert_logsync(live.store)
    assert_lockfree(live.store)


def test_final_valuation_uses_one_source_for_everything(live: World) -> None:
    """TS CMD-C02 "one source": Yahoo fails JPM, so the whole valuation (and the benchmark) is Finnhub's."""
    live.prices("yahoo", JPM="timeout")
    live.prices("finnhub", AAPL=Decimal(220))

    assert live.engine().run_final_valuation(benchmark="SPY").outcome == "completed"

    report = live.store.load_portfolio().final_report
    assert report is not None and report["source"] == "finnhub" and Decimal(report["ending_value"]) == Decimal(52000)


def test_cmd_c05_final_valuation_with_an_unpriceable_holding_fails_closed(live: World) -> None:
    """TS CMD-C05: no price for MSFT on either source -> nothing is written, status stays LIVE, the user can retry."""
    live.prices("yahoo", MSFT="timeout")
    live.prices("finnhub", MSFT="http_error")
    handle("CLOSE", owner(CLOSE_AT), ctx(live))
    before = workspace_hashes(live.root)

    assert live.engine().run_final_valuation(benchmark="SPY").outcome == "failed_closed"

    assert workspace_hashes(live.root) == before
    assert live.store.load_portfolio().status == "LIVE"
    assert (live.root / "state" / "pending_close.json").exists()  # still pending: retry is possible
    assert_lockfree(live.store)


def test_cmd_c06_a_paused_book_can_be_closed(world: World) -> None:
    """TS CMD-C06: CLOSE is allowed from PAUSED."""
    world.seed(status="PAUSED")
    world.clock.set(CLOSE_AT)
    handle("CLOSE", owner(CLOSE_AT), ctx(world))
    assert world.engine().run_final_valuation(benchmark="SPY").outcome == "completed"
    assert world.store.load_portfolio().status == "CLOSED"


def test_cmd_c09_after_close_no_job_runs(live: World) -> None:
    """TS CMD-C09: advance through an evaluation slot and the open. No pricing, no Reasoner, nothing written."""
    handle("CLOSE", owner(CLOSE_AT), ctx(live))
    live.engine().run_final_valuation(benchmark="SPY")
    reasoner = StubReasoner(pair_output())
    evaluation = live.evaluation(reasoner)
    quotes_so_far = (len(live.yahoo.calls), len(live.finnhub.calls))
    before = workspace_hashes(live.root)

    for when in (EVAL_SAT, EXEC, EXEC + timedelta(days=5)):
        live.clock.set(when)
        assert tick(live.clock, live.store, Config(), execution=live.engine(), evaluation=evaluation) == 0

    assert workspace_hashes(live.root) == before
    assert (len(live.yahoo.calls), len(live.finnhub.calls)) == quotes_so_far
    assert reasoner.inputs == []
    assert proposal_state(live.store, PAIR_ID) == "CANCELLED_CLOSED"  # never revived, never filled


# -- the command path as wired ------------------------------------------------------------------------------------------------


def test_cmd_c02_close_confirm_records_a_final_report_and_sells_nothing(live: World) -> None:
    """TS CMD-C02: "``trades[]`` unchanged (positions are not sold)" and a final report. Today the command sells."""
    handle("CLOSE", owner(CLOSE_AT), ctx(live))
    n_before = len(live.store.load_portfolio().trades)

    assert handle("CLOSE CONFIRM", owner(CLOSE_AT + timedelta(minutes=2)), ctx(live)).startswith("✅")

    doc = live.store.load_portfolio()
    assert doc.status == "CLOSED"
    assert len(doc.trades) == n_before and doc.final_report is not None


def test_cmd_c08_open_proposals_are_cancelled_after_close_confirm_and_never_fill(live: World) -> None:
    """TS CMD-C08 (D19): after the command closes the book, the next tick's sweeper cancels the open proposals."""
    handle("CLOSE", owner(CLOSE_AT), ctx(live))
    assert handle("CLOSE CONFIRM", owner(CLOSE_AT + timedelta(minutes=2)), ctx(live)).startswith("✅")
    assert live.store.load_portfolio().status == "CLOSED"

    live.clock.set(EXEC)
    tick(live.clock, live.store, Config(), execution=live.engine())

    assert proposal_state(live.store, PAIR_ID) == proposal_state(live.store, SINGLE_ID) == "CANCELLED_CLOSED"
    fills = [t.trade_id for t in live.store.load_portfolio().trades if t.trade_id.startswith("P-2026-10-24")]
    assert fills == []


def test_cmd_c03_c04_confirm_needs_a_fresh_close(live: World) -> None:
    """TS CMD-C03 / CMD-C04 (D5): CONFIRM without CLOSE is refused; CONFIRM 11 minutes after CLOSE has lapsed."""
    before = workspace_hashes(live.root)
    assert handle("CLOSE CONFIRM", owner(CLOSE_AT), ctx(live)).startswith("❌")

    handle("CLOSE", owner(CLOSE_AT), ctx(live))
    reply = handle("CLOSE CONFIRM", owner(CLOSE_AT + timedelta(minutes=11)), ctx(live))

    assert reply.startswith("❌") and "expired" in reply.lower()
    assert live.store.load_portfolio().status == "LIVE"
    after = workspace_hashes(live.root)
    assert {k for k in after if after[k] != before.get(k)} == set()


def test_cmd_c10_only_the_requester_can_confirm(live: World) -> None:
    """TS CMD-C10: a different sender's CLOSE CONFIRM is refused and the owner's pending close stays pending."""
    cfg = Config(discord=DiscordConfig(allowed_user_ids=["111", "222"]))
    handle("CLOSE", Sender("discord", "111", "m1", CLOSE_AT), ctx(live, cfg))

    reply = handle("CLOSE CONFIRM", Sender("discord", "222", "m2", CLOSE_AT + timedelta(minutes=1)), ctx(live, cfg))

    assert reply.startswith("❌")
    assert live.store.load_portfolio().status == "LIVE" and (live.root / "state" / "pending_close.json").exists()


@pytest.mark.parametrize("status", ["STAGED", "APPROVED", "CLOSED"])
def test_cmd_c07_close_is_refused_outside_live_and_paused(world: World, status: str) -> None:
    """TS CMD-C07: CLOSE is allowed from LIVE and PAUSED only."""
    world.seed(status=status)
    before = workspace_hashes(world.root)
    assert handle("CLOSE", owner(CLOSE_AT), ctx(world)).startswith("❌")
    assert workspace_hashes(world.root) == before and not (world.root / "state" / "pending_close.json").exists()


def test_a_closed_book_refuses_every_state_changing_command(live: World) -> None:
    """After CLOSED, PAUSE/RESUME/CLOSE and APPROVE are ❌ and the books do not move."""
    handle("CLOSE", owner(CLOSE_AT), ctx(live))
    live.engine().run_final_valuation(benchmark="SPY")
    before = workspace_hashes(live.root)
    for text in ("PAUSE", "RESUME", "CLOSE", f"APPROVE {PAIR_ID}", f"VETO {PAIR_ID}"):
        assert handle(text, owner(EXEC), ctx(live)).startswith("❌"), text
    assert workspace_hashes(live.root) == before
    assert alerts(live.root, "system_pause_resume") == []
    assert_nofill(books(live.store), live.store)
