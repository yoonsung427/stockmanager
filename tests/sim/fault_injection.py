"""Fault injection: kill the process at each commit point and check the next run recovers (TS §3.3, NN-20..NN-29).

The crash points are TS §3.3's CP0..CP7 (``stockmanager.sim.CRASH_POINTS``), not the CP0..CP7 of the task brief: the
spec names CP0 ``after_lock`` and CP7 ``torn_log_line``. A kill is ``SimulatedKill``; the dead process leaves
``portfolio.lock`` behind, so every restart that is not specifically about the lock waits out the 15-minute staleness
rule (TS D13) with ``restart(16)``. Each case runs against PR-SINGLE (one fill) and PR-PAIR (two fills).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from stockmanager.commands.auth import Sender
from stockmanager.commands.handlers import CommandContext, handle
from stockmanager.config import Config
from stockmanager.dashboard import build_dashboard
from stockmanager.domain.docs import ProposalLeg, ProposalSize
from stockmanager.execution.engine import RunResult
from stockmanager.sim import CRASH_POINTS, SimulatedKill, StubReasoner
from stockmanager.store.reconcile import read_log_lines
from stockmanager.store.transaction import ReconcileHalt
from tests.support.assertions import (
    alerts,
    assert_audit,
    assert_lockfree,
    assert_logsync,
    assert_nofill,
    assert_notmp,
    assert_recon,
    books,
    proposal_state,
    workspace_hashes,
)
from tests.support.reasoner_output import pair_output
from tests.support.world import EVAL_SAT, EVAL_SLOT, PAIR_ID, SINGLE_ID, World, pr_pair, pr_single

SEED = 20261026  # NN-27 / NN-50 style property seed; fixed and logged in failure messages


@dataclass
class Case:
    """A LIVE book with one APPROVED proposal ready to execute."""

    world: World
    pid: str
    legs: int

    def fills(self) -> list[str]:
        """Trade ids of this proposal's fills in ``portfolio.json``."""
        return [t.trade_id for t in self.world.store.load_portfolio().trades if t.trade_id.startswith(self.pid)]

    def crash(self, point: str) -> None:
        """Run the execution job and die at ``point``."""
        inj = self.world.injector()
        with pytest.raises(SimulatedKill), inj.crash_at(point):
            self.world.engine().run_proposal(self.pid)
        assert inj.fired
        assert self.world.store.lock.is_held(), "a killed process cannot release portfolio.lock"

    def rerun(self, minutes: float = 16) -> RunResult:
        """Restart ``minutes`` later (default: past the stale-lock threshold) and run the job again."""
        self.world.restart(minutes)
        return self.world.engine().run_proposal(self.pid)


@pytest.fixture(params=["single", "pair"])
def case(request: pytest.FixtureRequest, world: World) -> Case:
    world.seed()
    if request.param == "pair":
        world.put(pr_pair())
        return Case(world, PAIR_ID, 2)
    world.put(pr_single())
    return Case(world, SINGLE_ID, 1)


def _assert_done_once(c: Case, *, lock_free: bool = True) -> None:
    """The proposal executed exactly once: one fill per leg, books consistent, proposal ``EXECUTED``."""
    assert len(c.fills()) == c.legs
    assert proposal_state(c.world.store, c.pid) == "EXECUTED"
    assert_recon(c.world.store)
    assert_logsync(c.world.store)
    if lock_free:
        assert_lockfree(c.world.store)
    assert_audit(c.world.store.load_portfolio(), c.world.store)


# -- NN-20 .. NN-26: one test per crash point ----------------------------------------------------------------


def test_nn20_crash_after_lock_aborts_until_the_lock_is_stale(case: Case) -> None:
    """TS NN-20 / WP-10 / WP-08 (D13): at 1 min the dead owner's lock is too young to break; at 16 min it is broken."""
    w = case.world
    case.crash("after_lock")
    before = workspace_hashes(w.root)

    w.restart(1)
    assert w.engine().run_proposal(case.pid).outcome == "lock_busy"
    assert workspace_hashes(w.root) == before  # A-UNCHANGED: no unlocked write
    assert w.store.lock.is_held()

    w.restart(15)  # 16 min after the crash
    assert w.engine().run_proposal(case.pid).outcome == "completed"
    _assert_done_once(case)
    assert len(list((w.root / "run-notes").glob("*lockbreak*"))) == 1  # the break is logged


def test_nn21_crash_with_tmp_written_ignores_and_removes_the_tmp(case: Case) -> None:
    """TS NN-21: ``portfolio.json.tmp`` is complete but never renamed; the restart does not read it and removes it."""
    w = case.world
    before = books(w.store)
    case.crash("tmp_written")
    assert (w.root / "portfolio.json.tmp").exists()
    assert_nofill(before, w.store)  # portfolio.json itself is untouched

    assert case.rerun().outcome == "completed"
    assert_notmp(w.root)
    _assert_done_once(case)


def test_nn22_crash_after_portfolio_rename_repairs_the_log_without_refilling(case: Case) -> None:
    """TS NN-22: books ahead of ``trades.log``; the restart appends the missing lines and does not fill again."""
    w = case.world
    case.crash("after_portfolio_rename")
    after_kill = books(w.store)
    assert len(case.fills()) == case.legs and len(read_log_lines(w.store.trades_log_path)) == 3  # seed lines only

    assert case.rerun().outcome == "completed"
    assert books(w.store).trade_ids == after_kill.trade_ids  # trades[] unchanged by the restart
    _assert_done_once(case)
    assert alerts(w.root, "reconcile_divergence") == []  # a repairable gap is not a halt


def test_nn22_log_repair_is_noted_in_run_notes(case: Case) -> None:
    """TS NN-22: "notes it in run-notes". Today the repair happens but nothing records it."""
    case.crash("after_portfolio_rename")
    case.rerun()
    notes = " ".join(p.read_text(encoding="utf-8") for p in (case.world.root / "run-notes").glob("*.md"))
    assert "repair" in notes.lower()


def test_nn23_crash_mid_log_append_restores_the_missing_leg(world: World) -> None:
    """TS NN-23 (pair only): the first log line is on disk, the second is not; the restart appends exactly that one."""
    world.seed()
    world.put(pr_pair())
    c = Case(world, PAIR_ID, 2)
    c.crash("mid_log_append")
    assert len(read_log_lines(world.store.trades_log_path)) == 4  # 3 seed lines + first leg

    assert c.rerun().outcome == "completed"
    lines = read_log_lines(world.store.trades_log_path)
    for leg in ("sell1", "buy1"):
        assert sum(line.startswith(f"{PAIR_ID}-{leg} | ") for line in lines) == 1  # each leg exactly once
    _assert_done_once(c)


def test_nn24_crash_after_log_is_completed_by_the_idempotency_guard(case: Case) -> None:
    """TS NN-24: both ids are already in ``trades[]``; the restart skips the fills and only finishes the proposal."""
    w = case.world
    case.crash("after_log")
    after_kill = books(w.store)
    assert proposal_state(w.store, case.pid) == "APPROVED"

    result = case.rerun()
    assert result.outcome == "completed" and "already executed" in result.detail
    assert books(w.store) == after_kill  # not one fill, log line or cash unit more
    _assert_done_once(case)


@pytest.mark.parametrize("point", ["after_proposal_state", "after_runnotes"])
def test_nn25_crash_after_proposal_state_leaves_consistent_books(case: Case, point: str) -> None:
    """TS NN-25 (CP5, CP6): the books are final; a later run is a no-op and the dashboard shows nothing ahead of them."""
    w = case.world
    case.crash(point)
    after_kill = books(w.store)
    assert proposal_state(w.store, case.pid) == "EXECUTED"

    assert case.rerun().outcome == "no_op"
    assert books(w.store) == after_kill
    _assert_done_once(case, lock_free=False)  # a no-op run never takes the lock, so the dead owner's lock lingers...
    assert w.store.lock.break_stale() is not None  # ...but it is stale (old, owner dead) and the next job breaks it
    assert_lockfree(w.store)
    model = build_dashboard(w.store, w.clock)  # regenerated from the books, so it cannot be ahead of them
    doc = w.store.load_portfolio()
    assert model.cash == doc.cash
    assert {t.trade_id for t in model.recent_trades} <= {t.trade_id for t in doc.trades}


def _run_expecting_halt(w: World, pid: str) -> None:
    """The job must stop on a reconcile divergence: either it returns ``halted_reconcile`` or ``ReconcileHalt`` escapes."""
    try:
        result = w.engine().run_proposal(pid)
    except ReconcileHalt:
        return
    assert result.outcome == "halted_reconcile"


def test_nn26_torn_log_line_halts_until_reconcile_rebuild(case: Case) -> None:
    """TS NN-26: a log line the books don't explain halts the job (no auto-repair) until ``RECONCILE rebuild``."""
    w = case.world
    case.crash("torn_log_line")
    after_kill = books(w.store)
    torn = w.store.trades_log_path.read_bytes()
    assert not torn.endswith(b"\n")

    w.restart(16)
    _run_expecting_halt(w, case.pid)
    assert_nofill(after_kill, w.store)  # A-NOFILL
    assert w.store.trades_log_path.read_bytes() == torn  # no automatic repair
    assert proposal_state(w.store, case.pid) == "APPROVED"  # status unchanged
    assert_lockfree(w.store)

    ctx = CommandContext(w.store, Config(), w.clock, w.calendar, w.yahoo)
    reply = handle("RECONCILE rebuild", Sender("cli", None, None, w.clock()), ctx)
    assert reply.startswith("✅"), reply

    w.restart(1)
    assert w.engine().run_proposal(case.pid).outcome == "completed"  # the halt is cleared
    _assert_done_once(case)


def test_nn26_divergence_alerts_the_owner(case: Case) -> None:
    """TS NN-26 / NN-41: A-NOTIFY(reconcile divergence, 1). ``reconcile_divergence`` exists as a template but is unused."""
    case.crash("torn_log_line")
    case.world.restart(16)
    _run_expecting_halt(case.world, case.pid)
    assert len(alerts(case.world.root, "reconcile_divergence")) == 1


# -- NN-27: the write-order invariant, randomised --------------------------------------------------------------


def _random_pair(rng: random.Random, pid: str) -> tuple[str, list[ProposalLeg]]:
    """PR-PAIR with a random sell fraction (5%..70% of AAPL), so fills differ run to run."""
    fraction = Decimal(rng.randint(5, 70)) / 100
    doc = pr_pair(pid)
    sell = doc.legs[0].model_copy(update={"size": ProposalSize(kind="fraction_of_position", fraction=fraction)})
    return pid, [sell, doc.legs[1]]


def test_nn27_the_log_is_never_ahead_of_the_books(tmp_path: Path, fast_fsync: None) -> None:
    """TS NN-27: 200 random kills; except at CP7, ``trades.log`` never holds an id missing from ``trades[]``."""
    rng = random.Random(SEED)
    for i in range(200):
        w = World(tmp_path / f"w{i}")
        w.seed()
        pid, legs = _random_pair(rng, PAIR_ID)
        w.put(pr_pair(pid).model_copy(update={"legs": legs}))
        point = rng.choice(CRASH_POINTS)
        inj = w.injector()
        try:
            with inj.crash_at(point):
                w.engine().run_proposal(pid)
        except SimulatedKill:
            pass
        where = f"seed={SEED} iteration={i} point={point}"
        doc = w.store.load_portfolio()
        ids = {t.trade_id for t in doc.trades}
        logged = {line.split(" | ")[0] for line in read_log_lines(w.store.trades_log_path)}
        if point != "torn_log_line":  # the torn fragment is the one permitted exception
            assert logged <= ids, where
        w.restart(16)
        try:
            w.engine().run_proposal(pid)
        except ReconcileHalt:
            assert point == "torn_log_line", where
            continue
        assert_recon(w.store)
        assert_logsync(w.store)


def test_nn28_restart_is_idempotent(case: Case) -> None:
    """TS NN-28: the job run five times back-to-back fills each leg once."""
    w = case.world
    assert w.engine().run_proposal(case.pid).outcome == "completed"
    after_first = books(w.store)
    for _ in range(5):
        w.clock.advance(seconds=10)
        assert w.engine().run_proposal(case.pid).outcome == "no_op"
        assert_nofill(after_first, w.store)
    _assert_done_once(case)


# -- NN-29: evaluation never touches the books ------------------------------------------------------------------


@pytest.fixture
def eval_world(tmp_path: Path, fast_fsync: None) -> World:
    w = World(tmp_path / "ws", now=EVAL_SAT)
    w.seed()
    return w


def _book_hashes(w: World) -> dict[str, str]:
    return {k: v for k, v in workspace_hashes(w.root).items() if k in ("portfolio.json", "trades.log")}


@pytest.mark.parametrize("point", ["after_lock", "after_proposal_state", "after_runnotes"])
def test_nn29_crash_during_evaluation_leaves_the_books_alone(eval_world: World, point: str) -> None:
    """TS NN-29: an evaluation kill leaves ``portfolio.json``/``trades.log`` byte-identical; the next run completes."""
    w = eval_world
    before = _book_hashes(w)
    reasoner = StubReasoner(pair_output())
    inj = w.injector()
    with pytest.raises(SimulatedKill), inj.crash_at(point):
        w.evaluation(reasoner).run(EVAL_SLOT, Config())
    assert _book_hashes(w) == before

    w.restart(16)
    assert w.evaluation(reasoner).run(EVAL_SLOT, Config()).outcome == "completed"
    assert _book_hashes(w) == before
    assert_lockfree(w.store)
    assert list((w.root / "proposals").glob("*.json"))


def test_nn29_retry_after_crash_does_not_duplicate_the_proposal(eval_world: World) -> None:
    """TS NN-29: "the next run overwrites". The crashed run's proposal is on disk but its alert was never queued, so
    the retry builds ``P-2026-10-24-02`` next to an orphan ``-01`` that the user was never told about (cf. AC-02)."""
    w = eval_world
    reasoner = StubReasoner(pair_output())
    with pytest.raises(SimulatedKill), w.injector().crash_at("after_proposal_state"):
        w.evaluation(reasoner).run(EVAL_SLOT, Config())
    w.restart(16)
    w.evaluation(reasoner).run(EVAL_SLOT, Config())
    assert len(list((w.root / "proposals").glob("*.json"))) == 1
