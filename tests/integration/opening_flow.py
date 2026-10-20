"""Opening flow: STAGED -> APPROVED -> opening fills -> LIVE (TS E2E-06, NN-15, NN-36, EDGE-30, PRC-01, CMD-A03).

The opening job fills each staged pick independently at the session open (one source per pick), all-or-nothing:
if any pick cannot be priced nothing is written.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from stockmanager.commands.auth import Sender
from stockmanager.commands.handlers import CommandContext, handle
from stockmanager.config import Config
from stockmanager.domain.docs import PortfolioDoc
from stockmanager.scheduler.tick import tick
from stockmanager.sim import SimulatedKill
from tests.support.assertions import (
    alerts,
    assert_audit,
    assert_lockfree,
    assert_logsync,
    assert_nofill,
    assert_recon,
    books,
    workspace_hashes,
)
from tests.support.world import SESSION_OPEN, World

SUNDAY = datetime(2026, 10, 25, 12, 0, tzinfo=SESSION_OPEN.tzinfo)
PICKS = [
    {"ticker": "AAPL", "target_value": "10000", "thesis": "Services mix."},
    {"ticker": "MSFT", "target_value": "10000", "thesis": "Cloud."},
    {"ticker": "JPM", "target_value": "5000", "thesis": "Rates."},
]
OPEN_IDS = [f"P-OPEN-2026-10-26-buy{n}" for n in (1, 2, 3)]


def staged(w: World, status: str) -> PortfolioDoc:
    """S-STAGED / S-APPROVED: 50,000 cash, no fills, three picks. AAPL at 187.35 makes PRC-01's rounding visible."""
    doc = w.seed_trades([], "50000", status)
    doc = doc.model_copy(update={"config": {"staged_orders": PICKS}})
    w.store.save_portfolio(doc)
    for provider in (w.yahoo, w.finnhub):
        provider.script["AAPL"] = Decimal("187.35")
    return doc


def run_tick(w: World) -> int:
    """One dispatcher tick with the real execution engine."""
    return tick(w.clock, w.store, Config(), execution=w.engine())


def test_e2e06_approved_book_goes_live_with_one_independent_fill_per_pick(world: World) -> None:
    """TS E2E-06 / PRC-01 / PRC-02: no fill before the open; at the open three fills, 4 dp half-up, cash = start - Σ cost."""
    staged(world, "APPROVED")
    world.clock.set(SUNDAY)
    run_tick(world)  # the book is APPROVED but the market is closed
    assert world.store.load_portfolio().status == "APPROVED" and world.store.load_portfolio().trades == []

    world.clock.set(SESSION_OPEN)  # the exact instant; the test below covers any later moment
    assert run_tick(world) == 0

    doc = world.store.load_portfolio()
    assert doc.status == "LIVE"
    assert [t.trade_id for t in doc.trades] == OPEN_IDS
    aapl, msft, jpm = doc.trades
    assert (aapl.shares, aapl.cost) == (Decimal("53.3760"), Decimal("9999.9936"))  # PRC-01
    assert (msft.shares, jpm.shares) == (Decimal("25.0000"), Decimal("50.0000"))
    assert {t.timestamp for t in doc.trades} == {SESSION_OPEN}
    assert doc.cash == Decimal(50000) - sum((t.cost for t in doc.trades), Decimal(0)) == Decimal("25000.0064")
    assert [p.ticker for p in doc.positions] == ["AAPL", "MSFT", "JPM"]
    assert_audit(doc, world.store)
    assert_recon(world.store)
    assert_logsync(world.store)
    assert_lockfree(world.store)
    assert len(alerts(world.root, "trade_executed")) == 3
    assert world.store.nav_history_path.read_text(encoding="utf-8").count('"kind":"inception"') == 1


@pytest.mark.parametrize("delay", [timedelta(seconds=1), timedelta(minutes=5)])
def test_e2e06_opening_fills_when_the_tick_lands_just_after_the_open(world: World, delay: timedelta) -> None:
    """TS E2E-06 / EDGE-04: a cron tick never lands on 06:30:00.000. The fill must still happen, stamped 06:30."""
    staged(world, "APPROVED")
    world.clock.set(SESSION_OPEN + delay)
    run_tick(world)
    doc = world.store.load_portfolio()
    assert doc.status == "LIVE" and {t.timestamp for t in doc.trades} == {SESSION_OPEN}


def test_cmd_a03_approve_opening_moves_staged_to_approved_without_a_fill(world: World) -> None:
    """TS CMD-A03 / E2E-06: ``APPROVE OPENING`` on S-STAGED, on a Sunday -> ✅ and status APPROVED, no fills yet."""
    staged(world, "STAGED")
    world.clock.set(SUNDAY)
    before = books(world.store)
    ctx = CommandContext(world.store, Config(), world.clock, world.calendar, world.yahoo)

    reply = handle("APPROVE OPENING", Sender("cli", None, None, SUNDAY), ctx)

    assert reply.startswith("✅") and world.store.load_portfolio().status == "APPROVED"
    assert_nofill(before, world.store)


def test_a_staged_book_is_never_filled_even_at_the_open(world: World) -> None:
    """TS E2E-06 "no fill before approval": status STAGED at 06:30:00 -> the tick does nothing."""
    staged(world, "STAGED")
    world.clock.set(SESSION_OPEN)
    before = workspace_hashes(world.root)
    run_tick(world)
    assert workspace_hashes(world.root) == before


def test_nn15_one_unpriceable_pick_means_no_opening_fills(world: World) -> None:
    """TS NN-15: MSFT has no price on either source. Nothing is written, status stays APPROVED, one alert names it."""
    staged(world, "APPROVED")
    world.prices("yahoo", MSFT="timeout")
    world.prices("finnhub", MSFT="malformed")
    world.clock.set(SESSION_OPEN)
    before = workspace_hashes(world.root)

    assert world.engine().run_opening().outcome == "failed_closed"

    after = workspace_hashes(world.root)
    assert {k for k in after if after[k] != before.get(k)} == set()
    doc = world.store.load_portfolio()
    assert doc.status == "APPROVED" and doc.trades == [] and doc.positions == []
    (alert,) = alerts(world.root, "price_source_failure")
    assert "MSFT" in alert["fields"]["tickers"]


@pytest.mark.parametrize("status", ["STAGED", "PAUSED", "CLOSED", "LIVE"])
def test_nn36_the_opening_job_refuses_any_status_but_approved(world: World, status: str) -> None:
    """TS NN-36: gate on ``APPROVED``. Nothing is priced, written or filled for any other status."""
    staged(world, status)
    world.clock.set(SESSION_OPEN)
    before = workspace_hashes(world.root)

    assert world.engine().run_opening().outcome == "skipped_not_live"

    assert workspace_hashes(world.root) == before
    assert world.yahoo.calls == [] and world.finnhub.calls == []


def test_edge30_independent_opening_fills_may_use_different_sources(world: World) -> None:
    """TS EDGE-30: AAPL prices on Yahoo; MSFT fails on Yahoo and falls back to Finnhub. Each fill records its own source."""
    staged(world, "APPROVED")
    world.prices("yahoo", MSFT="timeout")
    world.clock.set(SESSION_OPEN)

    assert world.engine().run_opening().outcome == "completed"

    sources = {t.ticker: t.price_source for t in world.store.load_portfolio().trades}
    assert sources == {"AAPL": "yahoo", "MSFT": "finnhub", "JPM": "yahoo"}
    assert_audit(world.store.load_portfolio(), world.store)


def test_opening_runs_once(world: World) -> None:
    """After LIVE the opening job is a no-op: running it again (or ticking) does not fill a second time."""
    staged(world, "APPROVED")
    world.clock.set(SESSION_OPEN)
    world.engine().run_opening()
    after_first = books(world.store)

    assert world.engine().run_opening().outcome == "skipped_not_live"
    run_tick(world)

    assert_nofill(after_first, world.store)


def test_a_crash_after_the_books_are_written_leaves_a_log_the_next_job_repairs(world: World) -> None:
    """TS NN-22 for the opening: killed between ``portfolio.json`` and ``trades.log``. The book is LIVE (the job will not
    refill) and the next transaction's reconcile appends the three missing log lines."""
    staged(world, "APPROVED")
    world.clock.set(SESSION_OPEN)
    inj = world.injector()
    with pytest.raises(SimulatedKill), inj.crash_at("after_portfolio_rename"):
        world.engine().run_opening()
    assert len(world.store.load_portfolio().trades) == 3 and world.store.trades_log_path.read_text(encoding="utf-8") == ""

    world.restart(16)
    assert world.engine().run_opening().outcome == "skipped_not_live"  # LIVE already: no second set of fills
    with world.store.transaction("next-job") as txn:
        txn.reconcile()
        assert txn.repaired_log_ids == OPEN_IDS

    assert len(world.store.load_portfolio().trades) == 3
    assert_logsync(world.store)
    assert_recon(world.store)
