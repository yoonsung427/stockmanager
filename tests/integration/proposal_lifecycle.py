"""Proposal lifecycle: PROPOSED -> VETO_WINDOW -> APPROVED -> EXECUTED, and the VETOED branch (TS E2E-02..04, NN-01..06).

Driven through the real dispatcher (``tick``), sweeper, command handlers and execution engine on a fake clock, with a
proposal that the real evaluation engine created, so the transition chain under test is the one production writes.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from stockmanager.commands.auth import Sender
from stockmanager.commands.handlers import CommandContext, handle
from stockmanager.config import Config
from stockmanager.config.model import DiscordConfig
from stockmanager.domain.docs import ProposalDoc
from stockmanager.domain.proposal_state import GENESIS_HASH, chain_hash
from stockmanager.execution.engine import ExecutionEngine
from stockmanager.scheduler.tick import tick
from stockmanager.sim import RecordingChannel, StubReasoner
from stockmanager.sim.clock import PT
from stockmanager.store import FileStore
from tests.support.assertions import (
    alerts,
    assert_audit,
    assert_lockfree,
    assert_logsync,
    assert_nofill,
    assert_recon,
    books,
    proposal_state,
)
from tests.support.reasoner_output import pair_output
from tests.support.world import EVAL_SAT, EXEC, PAIR_ID, WIN_CLOSE, World, pr_pair

SEED = 20261026
MON_EXEC_IDS = (f"{PAIR_ID}-sell1", f"{PAIR_ID}-buy1")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A PT instant in the week of the reference dates (Sat 10-24 .. Mon 10-26)."""
    return datetime(2026, 10, day, hour, minute, tzinfo=PT)


class Run:
    """One weekly cycle's wiring: seeded book, evaluation + execution engines, and a ``step`` that sets the clock and ticks."""

    def __init__(self, world: World, cfg: Config | None = None) -> None:
        world.seed()
        world.clock.set(EVAL_SAT)
        self.world = world
        self.cfg = cfg or Config()
        self.evaluation = world.evaluation(StubReasoner(pair_output()))
        self.channels = [RecordingChannel("discord", world.clock)]  # a visible channel: its receipts open the veto window

    def step(self, when: datetime) -> None:
        """Move the clock to ``when`` and run one tick against the current store."""
        self.world.clock.set(when)
        assert tick(
            self.world.clock, self.world.store, self.cfg, execution=self.world.engine(), evaluation=self.evaluation, channels=self.channels
        ) == 0

    @property
    def state(self) -> str:
        return proposal_state(self.world.store, PAIR_ID)

    def command(self, text: str, sent_at: datetime, user: str | None = None) -> str:
        """Run a command as the owner (CLI), or as Discord user ``user``, stamped ``sent_at`` (TS D11)."""
        sender = Sender("cli", None, None, sent_at) if user is None else Sender("discord", user, f"m-{sent_at:%H%M}", sent_at)
        ctx = CommandContext(self.world.store, self.cfg, self.world.clock, self.world.calendar, self.world.yahoo)
        return handle(text, sender, ctx)


def chain_ok(doc: ProposalDoc) -> bool:
    """The hash chain of ``transitions[]`` verifies end to end (AS §3.3)."""
    prev = GENESIS_HASH
    for n, t in enumerate(doc.transitions, start=1):
        if t.seq != n or t.prev_hash != prev or t.hash != chain_hash(prev, t.model_dump(mode="json", by_alias=True)):
            return False
        prev = t.hash
    return doc.state == doc.transitions[-1].to


def test_e2e02_no_veto_runs_the_whole_lifecycle_and_fills_at_the_open(world: World) -> None:
    """TS E2E-02: PROPOSED -> VETO_WINDOW -> APPROVED -> EXECUTED in order; both fills after the window, one source."""
    run = Run(world)
    run.step(EVAL_SAT)
    assert run.state == "PROPOSED"
    run.step(EVAL_SAT + timedelta(minutes=1))
    assert run.state == "VETO_WINDOW"
    run.step(WIN_CLOSE)  # one minute before the (delivery-bound) deadline of 13:01
    assert run.state == "VETO_WINDOW"
    run.step(WIN_CLOSE + timedelta(minutes=2))
    assert run.state == "APPROVED"
    before_open = books(world.store)
    run.step(at(25, 12))  # Sunday: approved, but no session
    assert run.state == "APPROVED"
    assert_nofill(before_open, world.store)

    run.step(EXEC)

    doc = world.proposal(PAIR_ID)
    assert [t.to for t in doc.transitions] == ["PROPOSED", "VETO_WINDOW", "APPROVED", "EXECUTED"]
    assert [t.actor.split(":")[0] for t in doc.transitions] == ["system"] * 4
    assert chain_ok(doc)
    fills = [t for t in world.store.load_portfolio().trades if t.trade_id in MON_EXEC_IDS]
    assert [t.trade_id for t in fills] == list(MON_EXEC_IDS)
    assert len({t.price_source for t in fills}) == 1
    assert all(t.timestamp >= WIN_CLOSE for t in fills)
    assert world.store.load_portfolio().cash >= 0
    assert_audit(world.store.load_portfolio(), world.store)
    assert_recon(world.store)
    assert_logsync(world.store)
    assert_lockfree(world.store)
    assert len(alerts(world.root, "trade_executed")) == 2


def test_e2e03_a_veto_in_the_window_stops_both_legs(world: World) -> None:
    """TS E2E-03 / NN-01 / NN-04 / NN-06: VETO at 10:00 -> ✅, VETOED with the reason; nothing fills at the open, even if the
    execution job is forced, even after a restart; no leg id exists anywhere in the books."""
    run = Run(world)
    run.step(EVAL_SAT)
    run.step(EVAL_SAT + timedelta(minutes=1))
    before = books(world.store)

    assert run.command(f"VETO {PAIR_ID} too tech-heavy", at(24, 10)).startswith("✅")

    doc = world.proposal(PAIR_ID)
    assert doc.state == "VETOED" and doc.transitions[-1].cause == "too tech-heavy" and chain_ok(doc)
    run.step(WIN_CLOSE + timedelta(minutes=2))
    run.step(EXEC)
    assert run.state == "VETOED"
    assert world.engine().run_proposal(PAIR_ID).outcome == "no_op"  # NN-01: forced job logs "VETOED, skipping"
    world.restart(1)  # NN-06
    assert world.engine().run_proposal(PAIR_ID).outcome == "no_op"
    assert_nofill(before, world.store)
    for text in (world.store.portfolio_path.read_text(encoding="utf-8"), world.store.trades_log_path.read_text(encoding="utf-8")):
        assert "-sell1" not in text.replace("P-OPEN", "") and f"{PAIR_ID}-buy1" not in text
    assert_lockfree(world.store)


def test_nn02_a_veto_after_the_window_but_before_the_fill_still_wins(world: World) -> None:
    """TS NN-02 (D11): the window closed at 13:00 and the sweeper approved it; a VETO stamped 14:00 still stops the fill."""
    run = Run(world)
    run.step(EVAL_SAT)
    run.step(EVAL_SAT + timedelta(minutes=1))
    run.step(at(24, 14, 0))
    assert run.state == "APPROVED"
    before = books(world.store)

    assert run.command(f"VETO {PAIR_ID}", at(24, 14, 0)).startswith("✅")
    run.step(EXEC)

    assert run.state == "VETOED"
    assert_nofill(before, world.store)


def test_nn05_a_veto_from_a_stranger_is_rejected_and_the_trade_executes(world: World) -> None:
    """TS NN-05: only an allow-listed sender can veto; the stranger gets ❌, the proposal is untouched and fills."""
    run = Run(world, Config(discord=DiscordConfig(allowed_user_ids=["111"])))
    run.step(EVAL_SAT)
    run.step(EVAL_SAT + timedelta(minutes=1))

    reply = run.command(f"VETO {PAIR_ID}", at(24, 10), user="999")

    assert reply.startswith("❌") and run.state == "VETO_WINDOW"
    run.step(WIN_CLOSE + timedelta(minutes=2))
    run.step(EXEC)
    assert run.state == "EXECUTED"
    assert_recon(world.store)


@pytest.mark.parametrize("verb", ["VETO", "APPROVE"])
def test_a_terminal_proposal_refuses_further_commands(world: World, verb: str) -> None:
    """TS D11 / EDGE-21: once EXECUTED nothing can veto or approve it (an auto proposal has nothing to approve); ❌, file untouched."""
    run = Run(world)
    for when in (EVAL_SAT, EVAL_SAT + timedelta(minutes=1), WIN_CLOSE + timedelta(minutes=2), EXEC):
        run.step(when)
    assert run.state == "EXECUTED"
    raw = world.store.proposal_path(PAIR_ID).read_bytes()

    reply = run.command(f"{verb} {PAIR_ID}", EXEC)

    assert reply.startswith("❌")
    assert world.store.proposal_path(PAIR_ID).read_bytes() == raw


def test_e2e04_approval_mode_expires_without_approve_and_fills_with_it(world: World, tmp_path_factory: pytest.TempPathFactory) -> None:
    """TS E2E-04: ``auto_approve: false``. Run 1 (silence): EXPIRED at the grace limit, nothing filled. Run 2 (APPROVE at
    14:00): fills at the open."""
    silent = Run(world, Config(auto_approve=False))
    silent.step(EVAL_SAT)
    silent.step(EVAL_SAT + timedelta(minutes=1))
    assert world.proposal(PAIR_ID).approval_mode == "explicit"
    silent.step(WIN_CLOSE + timedelta(minutes=2))
    assert silent.state == "VETO_WINDOW"  # no auto-approval at the deadline
    before = books(world.store)
    silent.step(EXEC)
    silent.step(at(26, 7, 31))
    assert silent.state == "EXPIRED"
    assert_nofill(before, world.store)

    approved = Run(World(tmp_path_factory.mktemp("approved") / "ws"), Config(auto_approve=False))
    approved.step(EVAL_SAT)
    approved.step(EVAL_SAT + timedelta(minutes=1))
    assert approved.command(f"APPROVE {PAIR_ID}", at(24, 14)).startswith("✅")
    approved.step(EXEC)
    assert approved.state == "EXECUTED"
    assert_audit(approved.world.store.load_portfolio(), approved.world.store)


def test_nn03_a_veto_racing_the_execution_job_has_exactly_one_winner(tmp_path: Path, fast_fsync: None) -> None:
    """TS NN-03: 200 rounds of ``VETO`` and the execution job released together with random 0-50 ms offsets. Each round ends
    VETOED with no fill, or EXECUTED with both fills and a ❌ for the veto; never both, never neither."""
    rng = random.Random(SEED)
    for i in range(200):
        w = World(tmp_path / f"r{i}")
        w.seed()
        w.put(pr_pair())
        quiet = lambda _s: None
        exec_engine = ExecutionEngine(
            FileStore(w.root, clock=w.clock, pid_checker=w.pids, sleep=quiet), w.clock, w.calendar, w.yahoo, w.finnhub
        )
        ctx = CommandContext(FileStore(w.root, clock=w.clock, pid_checker=w.pids, sleep=quiet), Config(), w.clock, w.calendar, w.yahoo)
        barrier = threading.Barrier(2)
        offsets = (rng.random() * 0.05, rng.random() * 0.05)
        replies: list[str] = []

        def veto(ctx: CommandContext = ctx, barrier: threading.Barrier = barrier, delay: float = offsets[0], out: list[str] = replies) -> None:
            barrier.wait()
            time.sleep(delay)
            out.append(handle(f"VETO {PAIR_ID} race", Sender("cli", None, None, EXEC), ctx))

        def execute(eng: ExecutionEngine = exec_engine, barrier: threading.Barrier = barrier, delay: float = offsets[1]) -> None:
            barrier.wait()
            time.sleep(delay)
            eng.run_proposal(PAIR_ID)

        threads = [threading.Thread(target=veto), threading.Thread(target=execute)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        where = f"seed={SEED} round={i} offsets={offsets} reply={replies}"
        state = proposal_state(w.store, PAIR_ID)
        fills = [t.trade_id for t in w.store.load_portfolio().trades if t.trade_id.startswith(PAIR_ID)]
        assert state in ("VETOED", "EXECUTED"), where
        if state == "VETOED":
            assert fills == [] and replies[0].startswith("✅"), where
        else:
            assert sorted(fills) == sorted(MON_EXEC_IDS) and replies[0].startswith("❌"), where
        assert_recon(w.store)
        assert_logsync(w.store)
        assert_lockfree(w.store)
