"""Negative tests: input the system must refuse, and the books it must leave alone (TS §3.1-§3.2, §4, NN-D12).

The task brief cites ``TS §4 — NEG-*``; the spec has no NEG ids. These are its negative-path rows: WP-01/02/06/09/10/11,
NN-07, NN-D12 and the cash invariant behind D1/AD14. TS has no row for invalid symbols; they are SS §8.2 / AS §8.2.
"""

from __future__ import annotations

import os
import random
import threading
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from stockmanager.config import Config
from stockmanager.config.proposals import ProposalConfig
from stockmanager.domain import metrics
from stockmanager.domain.docs import PortfolioDoc, Trade
from stockmanager.domain.fills import clamp_to_cash
from stockmanager.evaluation import ProposalBuilder
from stockmanager.evaluation.builder import BuildResult
from stockmanager.evaluation.reasoner_io import ReasonerOutput
from stockmanager.execution.engine import ExecutionEngine
from stockmanager.pricing.service import PriceService
from stockmanager.scheduler.sweeper import sweep
from stockmanager.sim import FakeSymbolVerifier, StubReasoner
from stockmanager.store import FileStore
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
from tests.support.books import make_trade
from tests.support.reasoner_output import buy_proceeds_leg, buy_value_leg, idea, output, sell_leg
from tests.support.world import (
    EVAL_SAT,
    EVAL_SLOT,
    EXPIRES,
    P_BASE,
    PAIR_ID,
    SINGLE_ID,
    World,
    pr_buy,
    pr_pair,
    pr_single,
)

SEED = 20261026


def build(w: World, out: ReasonerOutput, verifier: FakeSymbolVerifier | None = None, cfg: ProposalConfig | None = None) -> BuildResult:
    """Run ``ProposalBuilder`` on the seeded book at ``EVAL_SAT`` with a full valuation (holdings + SPY)."""
    w.clock.set(EVAL_SAT)
    valuation = PriceService().valuation(["AAPL", "JPM", "MSFT", "SPY"], date(2026, 10, 23), w.yahoo, w.finnhub)
    builder = ProposalBuilder(w.calendar, verifier or FakeSymbolVerifier(P_BASE))
    return builder.build(out, w.store.load_portfolio(), valuation, cfg or ProposalConfig(), w.clock)


# -- invalid symbols ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("ticker", ["ZZZZ", "DELISTED"])
def test_symbol_the_verifier_rejects_is_never_proposed(world: World, ticker: str) -> None:
    """SS §8.2: a buy of an unheld ticker the venue does not trade (or has delisted) is dropped as ``invalid_symbol``."""
    world.seed()
    verifier = FakeSymbolVerifier(P_BASE)  # ZZZZ / DELISTED are not tradable
    result = build(world, output(idea(1, buy_value_leg("buy1", ticker, "1000"))), verifier)
    assert result.proposals == []
    assert [(n.reason, n.detail) for n in result.not_proposed] == [("invalid_symbol", ticker)]
    assert verifier.asked == [ticker]


@pytest.mark.parametrize("ticker", ["AAPL; DROP", "../etc", "toolongtickerx", ""])
def test_malformed_symbol_is_rejected_before_the_verifier_is_asked(world: World, ticker: str) -> None:
    """SS §8.2: a string that is not shaped like a ticker never reaches the network-backed verifier."""
    world.seed()
    verifier = FakeSymbolVerifier(["AAPL; DROP", "../etc", "toolongtickerx", ""])  # even if it said yes
    result = build(world, output(idea(1, buy_value_leg("buy1", ticker, "1000"))), verifier)
    assert result.proposals == [] and [n.reason for n in result.not_proposed] == ["invalid_symbol"]
    assert verifier.asked == []


def test_selling_an_unheld_ticker_is_dropped(world: World) -> None:
    """SS §8.2: long-only. A sell of something the book does not hold is ``short_or_oversell``."""
    world.seed()
    result = build(world, output(idea(1, sell_leg("sell1", "XOM", "0.5000"), buy_value_leg("buy1", "MSFT", "100"))))
    assert result.proposals == [] and [n.reason for n in result.not_proposed] == ["short_or_oversell"]


def test_new_symbol_buy_is_priced_and_proposed(world: World) -> None:
    """SS §8.2: a valid, unheld ticker is a legal buy. The engine never prices it, so the builder drops it ``unpriced``."""
    world.seed()
    world.clock.set(EVAL_SAT)
    for provider in (world.yahoo, world.finnhub):
        provider.script["XOM"] = Decimal(100)
    reasoner = StubReasoner(output(idea(1, buy_value_leg("buy1", "XOM", "2000"))))
    engine = world.evaluation(reasoner, verifier=FakeSymbolVerifier([*P_BASE, "XOM"]))
    assert engine.run(EVAL_SLOT, Config()).outcome == "completed"
    assert len(list((world.root / "proposals").glob("*.json"))) == 1


# -- cap breaches ------------------------------------------------------------------------------------------------


def test_wp11_overcap_buy_is_resized_to_the_cap_at_creation(world: World) -> None:
    """TS WP-11 (PR-OVERCAP): a 20,000 MSFT buy on an 8,000 position is cut so MSFT ends at no more than 30% of NAV."""
    world.seed()
    result = build(world, output(idea(1, buy_value_leg("buy1", "MSFT", "20000"))))
    assert result.not_proposed == []
    (proposal,) = result.proposals
    bought = proposal.legs[0].est_value
    assert bought <= Decimal(7000)  # 30% of 50,000 = 15,000, minus the 8,000 already held
    assert (Decimal(8000) + bought) / Decimal(50000) * 100 <= proposal.caps.max_position_pct


def test_wp11_buy_into_a_position_already_over_the_cap_is_dropped(world: World) -> None:
    """TS WP-11: AAPL is 40% of NAV (cap 30%); adding to it is dropped whole as ``position_cap``."""
    world.seed()
    result = build(world, output(idea(1, buy_value_leg("buy1", "AAPL", "1000"))))
    assert result.proposals == [] and [n.reason for n in result.not_proposed] == ["position_cap"]


def test_wp11_fourth_fill_is_listed_as_not_proposed(world: World) -> None:
    """TS WP-11 (PR-4LEG): ``max_trades_per_cycle`` is 3. A 4-leg idea is dropped; of two pairs, only the first is kept."""
    world.seed()
    four = idea(
        1,
        sell_leg("sell1", "AAPL", "0.1000"),
        sell_leg("sell2", "JPM", "0.1000"),
        buy_proceeds_leg("buy1", "MSFT", "sell1"),
        buy_proceeds_leg("buy2", "SPY", "sell2"),
    )
    result = build(world, output(four))
    assert result.proposals == [] and [n.reason for n in result.not_proposed] == ["trade_cap"]

    two_pairs = output(
        idea(1, sell_leg("sell1", "AAPL", "0.1000"), buy_proceeds_leg("buy1", "MSFT", "sell1")),
        idea(2, sell_leg("sell1", "JPM", "0.1000"), buy_proceeds_leg("buy1", "SPY", "sell1")),
    )
    result = build(world, two_pairs)
    assert len(result.proposals) == 1 and [(n.priority, n.reason) for n in result.not_proposed] == [(2, "trade_cap")]
    assert sum(len(p.legs) for p in result.proposals) <= 3


def test_nn_d12_cap_breached_at_execution_voids_the_whole_proposal(world: World) -> None:
    """TS NN-D12 (P-GAP-UP): JPM gaps from 100 to 135 at the open, so the buy would leave it above 30%; nothing fills."""
    world.seed()
    world.put(pr_buy("JPM", "9000", "90", "100"))
    world.prices("yahoo", JPM=Decimal(135))
    before = books(world.store)

    result = world.engine().run_proposal(SINGLE_ID)

    assert result.outcome == "voided" and "cap_breach" in result.detail
    assert_nofill(before, world.store)
    assert proposal_state(world.store, SINGLE_ID) == "VOIDED"
    assert_lockfree(world.store)


def test_nn_d12_void_alerts_the_owner(world: World) -> None:
    """TS NN-D12: "alert sent". The void is silent today; the owner learns only from the proposal file."""
    world.seed()
    world.put(pr_buy("JPM", "9000", "90", "100"))
    world.prices("yahoo", JPM=Decimal(135))
    world.engine().run_proposal(SINGLE_ID)
    assert alerts(world.root)


def test_sector_concentration_is_advisory_not_a_hard_cap(world: World) -> None:
    """SS §3 ``max_sector_pct``: "advisory alert, not a hard cap". It fires strictly above the threshold (TS D4) and
    never blocks a fill."""
    assert metrics.sector_concentration_alert(Decimal(80)) is False
    assert metrics.sector_concentration_alert(Decimal("80.0001")) is True

    world.seed()
    world.put(pr_pair())
    assert world.engine().run_proposal(PAIR_ID).outcome == "completed"
    doc = world.store.load_portfolio()
    values = {p.ticker: p.shares * P_BASE[p.ticker] for p in doc.positions}
    weights = metrics.sector_weights(values, {"AAPL": "Tech", "MSFT": "Tech", "JPM": "Fin"}, doc.cash)
    assert metrics.sector_concentration_alert(metrics.sector_concentration(weights), threshold=Decimal(50)) is True


# -- cash is never negative ------------------------------------------------------------------------------------


def test_cash_cannot_go_negative_in_the_document_model() -> None:
    """TS D1 / AS §3.1: ``PortfolioDoc`` rejects negative cash, so no code path can stage it."""
    with pytest.raises(ValidationError):
        PortfolioDoc(start_cash=Decimal(1000), cash=Decimal("-0.0001"), status="LIVE")


def test_a_buy_larger_than_cash_is_clamped_to_cash_never_below_zero(world: World) -> None:
    """AD14: JPM target 5,000 with 3,000 cash fills 30 shares and leaves exactly 0.0000, which still reconciles."""
    trades = [
        make_trade(1, ticker="AAPL", shares="100", price="200"),
        make_trade(2, ticker="JPM", shares="50", price="100"),
        make_trade(3, ticker="MSFT", shares="20", price="400"),
    ]
    world.seed_trades(trades, "36000")  # cash 3,000
    world.put(pr_buy("JPM", "5000", "50", "100"))

    assert world.engine().run_proposal(SINGLE_ID).outcome == "completed"

    doc = world.store.load_portfolio()
    assert doc.cash == Decimal("0.0000")
    assert [t.shares for t in doc.trades if t.trade_id == f"{SINGLE_ID}-buy1"] == [Decimal("30.0000")]
    assert_recon(world.store)
    assert_logsync(world.store)


def test_clamp_to_cash_never_exceeds_the_cash_available() -> None:
    """AD14, 200 seeded cases: ``shares * price <= available`` whatever the inputs."""
    rng = random.Random(SEED)
    for i in range(200):
        price = Decimal(rng.randint(1, 500_000)) / 100
        available = Decimal(rng.randint(0, 5_000_000)) / 100
        wanted = Decimal(rng.randint(0, 10_000_000)) / 1000
        got = clamp_to_cash(wanted, price, available)
        assert 0 <= got <= wanted, f"seed={SEED} i={i}"
        assert got * price <= available, f"seed={SEED} i={i}: {got} @ {price} > {available}"


# -- expired proposals never execute ---------------------------------------------------------------------------


def test_approved_proposal_past_expires_at_does_not_fill_and_is_expired_by_the_sweeper(world: World) -> None:
    """TS EDGE-05 / NN-10: at 07:31 the job refuses; the sweeper moves it to ``EXPIRED`` and alerts exactly once."""
    world.seed()
    world.put(pr_pair())
    before = books(world.store)
    world.clock.set(EXPIRES.replace(minute=31))

    assert world.engine().run_proposal(PAIR_ID).outcome == "no_op"
    assert_nofill(before, world.store)

    sweep(world.clock(), world.store, world.clock)
    sweep(world.clock(), world.store, world.clock)  # idempotent: no second transition, no second alert
    assert proposal_state(world.store, PAIR_ID) == "EXPIRED"
    assert len(alerts(world.root, "proposal_expired")) == 1
    assert_nofill(before, world.store)


def test_a_proposal_not_yet_in_its_window_does_not_fill(world: World) -> None:
    """The window opens ``exec_delay_minutes`` (5) after ``execute_at``; 06:34 is too early."""
    world.seed()
    world.put(pr_pair())
    before = books(world.store)
    world.clock.set(world.clock().replace(minute=34))
    assert world.engine().run_proposal(PAIR_ID).outcome == "no_op"
    assert_nofill(before, world.store)


# -- double fill ---------------------------------------------------------------------------------------------


def test_wp06_a_leg_already_in_trades_is_not_filled_twice(world: World) -> None:
    """TS WP-06 (PR-DUP-TRADE): ``P-…-01-sell1`` is already in ``trades[]``; the guard skips it and ``trades[]`` is unchanged."""
    base = [
        make_trade(1, ticker="AAPL", shares="100", price="200"),
        make_trade(2, ticker="JPM", shares="50", price="100"),
        make_trade(3, ticker="MSFT", shares="20", price="400"),
    ]
    sold = Trade(
        trade_id=f"{PAIR_ID}-sell1",
        timestamp=datetime(2026, 10, 26, 6, 30, tzinfo=base[0].timestamp.tzinfo),
        action="SELL",
        ticker="AAPL",
        shares=Decimal(25),
        fill_price=Decimal(200),
        cost=Decimal(5000),
        price_source="yahoo",
    )
    world.seed_trades([*base, sold], "50000")
    world.put(pr_pair())
    before = books(world.store)

    world.engine().run_proposal(PAIR_ID)

    after = books(world.store)
    assert after.trade_ids == before.trade_ids and after.log_lines == before.log_lines
    assert after.trade_ids.count(f"{PAIR_ID}-sell1") == 1
    assert_recon(world.store)


def test_nn07_a_hand_flipped_state_without_a_transition_record_does_not_execute(world: World) -> None:
    """TS NN-07: ``VETOED`` flipped to ``APPROVED`` by hand has no legal transition behind it; execution refuses."""
    world.seed()
    forged = pr_pair(state="VETOED").model_copy(update={"state": "APPROVED"})
    world.put(forged)
    before = books(world.store)

    result = world.engine().run_proposal(PAIR_ID)

    assert result.outcome == "aborted_state_changed" and "chain" in result.detail
    assert_nofill(before, world.store)
    assert world.proposal(PAIR_ID).transitions[-1].to == "VETOED"


def test_two_processes_racing_to_execute_fill_each_leg_once(tmp_path: Path, fast_fsync: None) -> None:
    """TS NN-28 / WP-01 under real contention: 25 rounds of two threads running the same job at once.

    The loser either finds the lock busy or runs after the winner and sees both ids already in ``trades[]``.
    """
    for i in range(25):
        w = World(tmp_path / f"w{i}")
        w.seed()
        w.put(pr_pair())
        engines = [
            ExecutionEngine(FileStore(w.root, clock=w.clock, pid_checker=w.pids, sleep=lambda _s: None), w.clock, w.calendar, w.yahoo, w.finnhub)
            for _ in range(2)
        ]
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def go(engine: ExecutionEngine, barrier: threading.Barrier = barrier, outcomes: list[str] = outcomes) -> None:
            barrier.wait()
            outcomes.append(engine.run_proposal(PAIR_ID).outcome)

        threads = [threading.Thread(target=go, args=(e,)) for e in engines]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert set(outcomes) <= {"completed", "lock_busy", "no_op"}, f"round {i}: {outcomes}"
        assert "completed" in outcomes, f"round {i}: {outcomes}"
        ids = [t.trade_id for t in w.store.load_portfolio().trades if t.trade_id.startswith(PAIR_ID)]
        assert sorted(ids) == [f"{PAIR_ID}-buy1", f"{PAIR_ID}-sell1"], f"round {i}"
        assert_recon(w.store)
        assert_logsync(w.store)
        assert_lockfree(w.store)


# -- lock contention ----------------------------------------------------------------------------------------


def _hold_lock_as_live_process(w: World) -> str:
    """Another job holds ``portfolio.lock`` and its process is alive. Returns the lock token."""
    pid = os.getpid()
    w.pids.alive_pids[pid] = w.pids.start_time(pid)
    return w.store.lock.acquire("evaluation", 1, 0).token


def test_wp01_the_loser_retries_three_times_then_aborts_without_writing(world: World) -> None:
    """TS WP-01: 3 attempts, ~10 s apart (so two sleeps), then it aborts; zero writes."""
    world.seed()
    world.put(pr_pair())
    token = _hold_lock_as_live_process(world)
    sleeps: list[float] = []
    loser = ExecutionEngine(
        FileStore(world.root, clock=world.clock, pid_checker=world.pids, sleep=sleeps.append),
        world.clock,
        world.calendar,
        world.yahoo,
        world.finnhub,
    )
    before = workspace_hashes(world.root)

    assert loser.run_proposal(PAIR_ID).outcome == "lock_busy"

    assert sleeps == [10.0, 10.0]
    assert workspace_hashes(world.root) == before
    meta = world.store.lock.read_meta()
    assert meta is not None and meta.token == token  # the winner's lock is untouched


def test_wp02_wp09_an_old_lock_with_a_live_owner_is_never_broken(world: World) -> None:
    """TS WP-02 / WP-09 (D13): 25 minutes old but its PID is alive, so the job aborts rather than breaking it."""
    world.seed()
    world.put(pr_pair())
    token = _hold_lock_as_live_process(world)
    world.clock.advance(minutes=25)  # 07:00: inside the proposal's window, far past the 15-minute staleness age
    before = workspace_hashes(world.root)

    assert world.engine().run_proposal(PAIR_ID).outcome == "lock_busy"

    assert workspace_hashes(world.root) == before
    meta = world.store.lock.read_meta()
    assert meta is not None and meta.token == token
    assert list(world.root.glob("portfolio.lock.broken.*")) == []


def test_wp10_a_young_lock_with_a_dead_owner_is_kept(world: World) -> None:
    """TS WP-10: owner dead but the lock is 1 minute old, so it is not stale yet and the job aborts."""
    world.seed()
    world.put(pr_pair())
    token = world.store.lock.acquire("evaluation", 1, 0).token  # nobody registered as alive: owner is dead
    world.clock.advance(minutes=1)
    assert world.engine().run_proposal(PAIR_ID).outcome == "lock_busy"
    meta = world.store.lock.read_meta()
    assert meta is not None and meta.token == token


def test_wp02_a_skipped_run_alerts_the_owner(world: World) -> None:
    """TS WP-02: A-NOTIFY(run skipped: portfolio busy, 1). The template exists; nothing emits it."""
    world.seed()
    world.put(pr_pair())
    _hold_lock_as_live_process(world)
    world.engine().run_proposal(PAIR_ID)
    assert len(alerts(world.root, "run_skipped_busy")) == 1


def test_single_proposal_fixture_is_a_legal_approved_proposal(world: World) -> None:
    """Guard the fixture itself: PR-SINGLE executes cleanly on S-LIVE, so every failure above is about the rule under test."""
    world.seed()
    world.put(pr_single())
    assert world.engine().run_proposal(SINGLE_ID).outcome == "completed"
    assert_recon(world.store)
