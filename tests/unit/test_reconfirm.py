"""RECONFIRM (AS §8.7, TS CMD-RC01..06, EDGE-21..23): the command, the derived ``reconfirm:<id>`` job, its dispatch."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest

from stockmanager.commands.handlers import CommandContext, cmd_reconfirm, handle
from stockmanager.config import Config
from stockmanager.domain.docs import PortfolioDoc, PriceQuote, PriceTable, ProposalDoc, ProposalSize
from stockmanager.domain.proposal_state import ProposalState
from stockmanager.evaluation.reconfirm import MAX_PENDING, ReconfirmEngine, ThesisVerdict
from stockmanager.pricing.errors import PriceError
from stockmanager.scheduler.scheduler import list_all_jobs, list_due_jobs
from stockmanager.scheduler.state import SchedulerState
from stockmanager.scheduler.sweeper import sweep
from stockmanager.scheduler.tick import tick
from stockmanager.store import FileStore
from tests.support.books import PT, FakePidChecker, FixedClock, make_portfolio, make_trade
from tests.support.proposals import make_leg, make_proposal

OLD = "P-2026-10-12-01"
NEW = "P-2026-10-12-02"  # the clock sits on 2026-10-12, so the next sequence number after OLD is 02


class FakeCalendar:
    def next_session_open_at_or_after(self, instant: datetime) -> datetime:
        return instant


class FakeVerifier:
    def is_valid_us_equity_or_etf(self, ticker: str) -> bool:
        return True


class FakeProvider:
    """Prices everything at ``prices`` (default 200); ``fail`` makes every call raise ``PriceError``."""

    def __init__(self, name: Literal["yahoo", "finnhub"] = "yahoo", price: str = "200") -> None:
        self.name = name
        self.price = Decimal(price)
        self.fail = False
        self.calls = 0

    def get_price(self, ticker: str, session_date: date, kind: Literal["open", "close"]) -> PriceQuote:
        self.calls += 1
        if self.fail:
            raise PriceError("timeout", ticker, "down")
        return PriceQuote(
            ticker=ticker, session_date=session_date, kind=kind, price=self.price, source=self.name, fetched_at=datetime.now(PT)
        )


class FakeThesis:
    def __init__(self, verdict: ThesisVerdict) -> None:
        self.verdict = verdict
        self.seen: list[str] = []

    def check(self, doc: ProposalDoc, valuation: PriceTable) -> ThesisVerdict:
        self.seen.append(doc.proposal_id)
        return self.verdict


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def store(tmp_path: Path, clock: FixedClock) -> FileStore:
    return FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)


@pytest.fixture
def cfg() -> Config:
    return Config(auto_approve=True)  # RECONFIRM must stay explicit regardless (EDGE-23)


@pytest.fixture
def ctx(store: FileStore, clock: FixedClock, cfg: Config) -> CommandContext:
    return CommandContext(store=store, cfg=cfg, clock=clock, calendar=FakeCalendar(), price_provider=FakeProvider())


@pytest.fixture
def primary() -> FakeProvider:
    return FakeProvider("yahoo")


@pytest.fixture
def fallback() -> FakeProvider:
    return FakeProvider("finnhub")


def engine(
    store: FileStore, clock: FixedClock, primary: FakeProvider, fallback: FakeProvider, thesis: FakeThesis | None = None
) -> ReconfirmEngine:
    return ReconfirmEngine(store, clock, FakeCalendar(), FakeVerifier(), primary, fallback, thesis)


def cli_sender(clock: FixedClock) -> Any:
    from stockmanager.commands.auth import Sender

    return Sender("cli", None, None, clock.now)


def seed(store: FileStore, state: ProposalState = "EXPIRED", target_value: str = "1000", status: str = "LIVE") -> None:
    """A LIVE portfolio (10 000 cash) plus the original proposal, buying ``target_value`` of AAPL."""
    store.save_portfolio(make_portfolio(status=status))
    doc = make_proposal(OLD, state=state, approval_mode="auto")
    leg = make_leg(pid=OLD).model_copy(update={"size": ProposalSize(kind="target_value", target_value=Decimal(target_value))})
    store.save_proposal(OLD, doc.model_copy(update={"legs": [leg]}))


def load(store: FileStore, pid: str) -> ProposalDoc:
    return ProposalDoc.model_validate_json(store.proposal_path(pid).read_text(encoding="utf-8"))


def proposal_files(store: FileStore) -> list[str]:
    return sorted(p.stem for p in (store.root / "proposals").glob("*.json"))


def alert_kinds(store: FileStore) -> list[str]:
    return sorted(json.loads(p.read_text())["kind"] for p in (store.root / "alerts").rglob("*.json"))


def snapshot_files(store: FileStore) -> dict[str, str]:
    return {str(p): p.read_text() for p in store.root.rglob("*.json") if "state" not in p.parts}


def reconfirm_pending(store: FileStore, clock: FixedClock, ctx: CommandContext) -> ProposalDoc:
    seed(store)
    assert cmd_reconfirm(ctx, OLD, cli_sender(clock)).startswith("✅")
    return load(store, NEW)


# -- the command: TS CMD-RC01..06 --------------------------------------------------------------------------------


def test_rc01_reconfirm_expired_creates_a_pending_explicit_replacement(
    store: FileStore, clock: FixedClock, ctx: CommandContext
) -> None:
    seed(store)
    reply = handle(f"RECONFIRM {OLD}", cli_sender(clock), ctx)
    assert reply.startswith("✅") and NEW in reply
    new = load(store, NEW)
    assert (new.state, new.approval_mode, new.reconfirms, new.reconfirm_check) == ("PROPOSED", "explicit", OLD, "pending")
    assert new.veto_deadline is None  # no clock runs before validation
    assert new.execute_at == clock.now + timedelta(hours=new.veto_window_hours)
    assert new.expires_at == new.execute_at + timedelta(minutes=ctx.cfg.proposal_grace_minutes)
    assert [leg.trade_id for leg in new.legs] == [f"{NEW}-buy1"]
    assert new.transitions[0].actor == "system:reconfirm"
    old = load(store, OLD)
    assert (old.state, old.reconfirmed_as) == ("EXPIRED", NEW)


def test_rc02_not_expired_is_refused_and_nothing_changes(store: FileStore, clock: FixedClock, ctx: CommandContext) -> None:
    seed(store, state="VETO_WINDOW")
    before = snapshot_files(store)
    reply = cmd_reconfirm(ctx, OLD, cli_sender(clock))
    assert reply.startswith("❌") and "Not expired" in reply
    assert snapshot_files(store) == before


@pytest.mark.parametrize("state", ["EXECUTED", "VETOED", "CANCELLED_PAUSED", "CANCELLED_CLOSED", "VOIDED"])
def test_rc03_other_terminal_states_are_refused(store: FileStore, clock: FixedClock, ctx: CommandContext, state: Any) -> None:
    seed(store, state=state)
    before = snapshot_files(store)
    assert cmd_reconfirm(ctx, OLD, cli_sender(clock)).startswith("❌")
    assert snapshot_files(store) == before


def test_rc04_second_reconfirm_is_refused_and_creates_no_second_file(
    store: FileStore, clock: FixedClock, ctx: CommandContext
) -> None:
    seed(store)
    assert cmd_reconfirm(ctx, OLD, cli_sender(clock)).startswith("✅")
    reply = cmd_reconfirm(ctx, OLD, cli_sender(clock))
    assert reply.startswith("❌") and f"already reconfirmed as {NEW}" in reply
    assert proposal_files(store) == [OLD, NEW]


def test_rc04_a_replacement_that_itself_expired_can_be_reconfirmed_again(
    store: FileStore, clock: FixedClock, ctx: CommandContext
) -> None:
    new = reconfirm_pending(store, clock, ctx)
    store.save_proposal(NEW, new.model_copy(update={"state": "EXPIRED", "reconfirm_check": "failed"}))
    assert cmd_reconfirm(ctx, NEW, cli_sender(clock)).startswith("✅")
    assert load(store, "P-2026-10-12-03").reconfirms == NEW


def test_rc05_unknown_id(store: FileStore, clock: FixedClock, ctx: CommandContext) -> None:
    seed(store)
    before = snapshot_files(store)
    assert cmd_reconfirm(ctx, "P-2026-10-12-09", cli_sender(clock)) == "❌ Unknown or expired proposal ID"
    assert snapshot_files(store) == before


def test_rc05_missing_or_malformed_id(store: FileStore, clock: FixedClock, ctx: CommandContext) -> None:
    seed(store)
    assert cmd_reconfirm(ctx, None, cli_sender(clock)).startswith("❌")
    assert cmd_reconfirm(ctx, "../etc/passwd", cli_sender(clock)).startswith("❌")
    assert proposal_files(store) == [OLD]


def test_rc06_paused_is_refused(store: FileStore, clock: FixedClock, ctx: CommandContext) -> None:
    seed(store, status="PAUSED")
    before = snapshot_files(store)
    assert cmd_reconfirm(ctx, OLD, cli_sender(clock)).startswith("❌")
    assert snapshot_files(store) == before


def test_rc06_halted_books_are_refused(store: FileStore, clock: FixedClock, ctx: CommandContext) -> None:
    seed(store)
    bad = make_portfolio([make_trade(1)])
    store.save_portfolio(bad.model_copy(update={"cash": bad.cash + 1}))  # cash disagrees with trades[]
    before = snapshot_files(store)
    assert cmd_reconfirm(ctx, OLD, cli_sender(clock)) == "❌ System halted"
    assert snapshot_files(store) == before


def test_reconfirm_writes_only_through_a_transaction(store: FileStore, clock: FixedClock, ctx: CommandContext) -> None:
    seed(store)
    held = store.lock.acquire("other", 1, 0)
    try:
        assert cmd_reconfirm(ctx, OLD, cli_sender(clock)) == "❌ System busy"
    finally:
        store.lock.release(held.token)
    assert proposal_files(store) == [OLD]
    assert not (store.root / "state" / "reconfirmed.json").exists()


# -- scheduler: the derived reconfirm:<id> job -------------------------------------------------------------------


def jobs_of(proposals: list[ProposalDoc], status: Any = "LIVE", due: bool = True) -> list[tuple[str, str | None]]:
    fn = list_due_jobs if due else list_all_jobs
    got = fn(FixedClock().now, Config(), status, proposals, SchedulerState())
    return [(j.kind, j.proposal_id) for j in got]


def test_job_exists_and_is_due_for_a_pending_proposed_replacement(
    store: FileStore, clock: FixedClock, ctx: CommandContext
) -> None:
    new = reconfirm_pending(store, clock, ctx)
    assert ("reconfirm", NEW) in jobs_of([new])
    assert ("reconfirm", NEW) in jobs_of([new], due=False)


@pytest.mark.parametrize(
    "update",
    [
        {"reconfirm_check": "passed"},
        {"reconfirm_check": "failed"},
        {"reconfirm_check": None},
        {"state": "VETO_WINDOW"},
        {"state": "EXPIRED"},
    ],
)
def test_job_does_not_exist_otherwise(
    store: FileStore, clock: FixedClock, ctx: CommandContext, update: dict[str, Any]
) -> None:
    new = reconfirm_pending(store, clock, ctx).model_copy(update=update)
    assert not any(kind == "reconfirm" for kind, _ in jobs_of([new]))
    assert not any(kind == "reconfirm" for kind, _ in jobs_of([new], due=False))


def test_job_ceases_while_paused_and_sorts_between_execution_and_evaluation(
    store: FileStore, clock: FixedClock, ctx: CommandContext
) -> None:
    new = reconfirm_pending(store, clock, ctx)
    assert not any(kind == "reconfirm" for kind, _ in jobs_of([new], status="PAUSED"))
    kinds = [k for k, _ in jobs_of([new])]
    assert kinds.index("reconfirm") < kinds.index("evaluation")


def test_sweeper_neither_delivers_nor_expires_a_pending_replacement(
    store: FileStore, clock: FixedClock, ctx: CommandContext
) -> None:
    new = reconfirm_pending(store, clock, ctx)
    clock.now = new.expires_at + timedelta(hours=5)  # long past its provisional expiry
    sweep(clock.now, store, clock)
    after = load(store, NEW)
    assert (after.state, after.veto_deadline) == ("PROPOSED", None)
    assert alert_kinds(store) == []


def test_sweeper_still_cancels_a_pending_replacement_when_paused(
    store: FileStore, clock: FixedClock, ctx: CommandContext
) -> None:
    reconfirm_pending(store, clock, ctx)
    store.save_portfolio(make_portfolio(status="PAUSED"))
    sweep(clock.now, store, clock)
    assert load(store, NEW).state == "CANCELLED_PAUSED"


# -- the job: fresh valuation, cap re-check, thesis re-check -----------------------------------------------------


def test_job_passes_and_opens_the_veto_window_still_explicit(
    store: FileStore, clock: FixedClock, ctx: CommandContext, cfg: Config, primary: FakeProvider, fallback: FakeProvider
) -> None:
    reconfirm_pending(store, clock, ctx)
    clock.advance(minutes=1)
    result = engine(store, clock, primary, fallback).run(NEW, cfg)
    assert result.outcome == "passed"
    new = load(store, NEW)
    assert (new.state, new.reconfirm_check, new.approval_mode) == ("VETO_WINDOW", "passed", "explicit")
    assert new.veto_deadline == clock.now + timedelta(hours=new.veto_window_hours)
    assert new.execute_at == clock.now + timedelta(hours=new.veto_window_hours)  # the clock started at validation
    assert (new.legs[0].est_price, new.legs[0].est_shares) == (Decimal("200.0000"), Decimal("5.0000"))
    assert [leg.trade_id for leg in new.legs] == [f"{NEW}-buy1"]
    last = new.transitions[-1]
    assert (last.from_state, last.to, last.actor) == ("PROPOSED", "VETO_WINDOW", "system:reconfirm")
    assert alert_kinds(store) == ["veto_window_open"]
    assert load(store, OLD).state == "EXPIRED"


def test_edge23_no_approve_means_it_expires_unfilled_even_with_auto_approve(
    store: FileStore, clock: FixedClock, ctx: CommandContext, cfg: Config, primary: FakeProvider, fallback: FakeProvider
) -> None:
    reconfirm_pending(store, clock, ctx)
    engine(store, clock, primary, fallback).run(NEW, cfg)
    new = load(store, NEW)
    assert new.veto_deadline is not None
    clock.now = new.veto_deadline + timedelta(minutes=1)
    sweep(clock.now, store, clock)
    assert load(store, NEW).state == "VETO_WINDOW"  # the deadline passing approves nothing
    clock.now = new.expires_at + timedelta(minutes=1)
    sweep(clock.now, store, clock)
    assert load(store, NEW).state == "EXPIRED"
    assert store.load_portfolio().trades == []


def test_edge22_a_buy_over_the_cap_is_resized(
    store: FileStore, clock: FixedClock, ctx: CommandContext, cfg: Config, primary: FakeProvider, fallback: FakeProvider
) -> None:
    seed(store, target_value="5000")  # 50% of a 10 000 NAV; the cap is 30%
    cmd_reconfirm(ctx, OLD, cli_sender(clock))
    assert engine(store, clock, primary, fallback).run(NEW, cfg).outcome == "passed"
    leg = load(store, NEW).legs[0]
    assert leg.est_value <= Decimal(3000) and leg.est_shares == Decimal("15.0000")


def test_edge22_a_buy_with_no_room_is_voided_with_an_alert_carrying_reconfirm(
    store: FileStore, clock: FixedClock, ctx: CommandContext, cfg: Config, primary: FakeProvider, fallback: FakeProvider
) -> None:
    seed(store)
    held = make_portfolio([make_trade(1, shares="20", price="200")], start_cash="10000")  # AAPL 4 000 of ~10 000 NAV
    store.save_portfolio(held)
    cmd_reconfirm(ctx, OLD, cli_sender(clock))
    result = engine(store, clock, primary, fallback).run(NEW, cfg)
    assert result.outcome == "voided" and "position_cap" in result.detail
    new = load(store, NEW)
    assert (new.state, new.reconfirm_check) == ("EXPIRED", "failed")
    assert alert_kinds(store) == ["proposal_expired"]
    alert = next(json.loads(p.read_text()) for p in (store.root / "alerts").rglob("*.json"))
    assert f"RECONFIRM {NEW}" in alert["commands"] and "position_cap" in alert["body"]


def test_price_failure_leaves_it_pending_and_the_next_tick_retries(
    store: FileStore, clock: FixedClock, ctx: CommandContext, cfg: Config, primary: FakeProvider, fallback: FakeProvider
) -> None:
    reconfirm_pending(store, clock, ctx)
    primary.fail = fallback.fail = True
    before = snapshot_files(store)
    assert engine(store, clock, primary, fallback).run(NEW, cfg).outcome == "pending"
    assert snapshot_files(store) == before  # fail closed
    primary.fail = False
    assert engine(store, clock, primary, fallback).run(NEW, cfg).outcome == "passed"


def test_one_source_per_valuation_falls_back_whole(
    store: FileStore, clock: FixedClock, ctx: CommandContext, cfg: Config, primary: FakeProvider, fallback: FakeProvider
) -> None:
    reconfirm_pending(store, clock, ctx)
    primary.fail = True
    fallback.price = Decimal(250)
    assert engine(store, clock, primary, fallback).run(NEW, cfg).outcome == "passed"
    assert load(store, NEW).legs[0].est_price == Decimal("250.0000")


def test_prices_unavailable_for_too_long_voids_it(
    store: FileStore, clock: FixedClock, ctx: CommandContext, cfg: Config, primary: FakeProvider, fallback: FakeProvider
) -> None:
    reconfirm_pending(store, clock, ctx)
    primary.fail = fallback.fail = True
    clock.advance(hours=MAX_PENDING.total_seconds() / 3600 + 1)
    assert engine(store, clock, primary, fallback).run(NEW, cfg).outcome == "voided"
    assert load(store, NEW).state == "EXPIRED"
    assert alert_kinds(store) == ["proposal_expired"]


def test_thesis_void_ends_it_with_the_reason(
    store: FileStore, clock: FixedClock, ctx: CommandContext, cfg: Config, primary: FakeProvider, fallback: FakeProvider
) -> None:
    reconfirm_pending(store, clock, ctx)
    thesis = FakeThesis(ThesisVerdict("void", "guidance was cut"))
    result = engine(store, clock, primary, fallback, thesis).run(NEW, cfg)
    assert result.outcome == "voided" and "guidance was cut" in result.detail
    assert thesis.seen == [NEW]
    assert load(store, NEW).state == "EXPIRED"


def test_thesis_keep_passes(
    store: FileStore, clock: FixedClock, ctx: CommandContext, cfg: Config, primary: FakeProvider, fallback: FakeProvider
) -> None:
    reconfirm_pending(store, clock, ctx)
    thesis = FakeThesis(ThesisVerdict("keep"))
    assert engine(store, clock, primary, fallback, thesis).run(NEW, cfg).outcome == "passed"
    assert load(store, NEW).transitions[-1].cause == "reconfirm_passed"


def test_without_a_thesis_checker_the_skip_is_recorded(
    store: FileStore, clock: FixedClock, ctx: CommandContext, cfg: Config, primary: FakeProvider, fallback: FakeProvider
) -> None:
    reconfirm_pending(store, clock, ctx)
    engine(store, clock, primary, fallback).run(NEW, cfg)
    assert "thesis re-check not configured" in load(store, NEW).transitions[-1].cause


def test_rerun_is_a_no_op(
    store: FileStore, clock: FixedClock, ctx: CommandContext, cfg: Config, primary: FakeProvider, fallback: FakeProvider
) -> None:
    reconfirm_pending(store, clock, ctx)
    eng = engine(store, clock, primary, fallback)
    assert eng.run(NEW, cfg).outcome == "passed"
    after = snapshot_files(store)
    assert eng.run(NEW, cfg).outcome == "no_op"
    assert snapshot_files(store) == after


def test_not_live_is_a_no_op(
    store: FileStore, clock: FixedClock, ctx: CommandContext, cfg: Config, primary: FakeProvider, fallback: FakeProvider
) -> None:
    reconfirm_pending(store, clock, ctx)
    store.save_portfolio(make_portfolio(status="PAUSED"))
    calls = primary.calls
    assert engine(store, clock, primary, fallback).run(NEW, cfg).outcome == "no_op"
    assert primary.calls == calls and load(store, NEW).state == "PROPOSED"


def test_state_change_during_the_check_aborts_and_retries(
    store: FileStore, clock: FixedClock, ctx: CommandContext, cfg: Config, primary: FakeProvider, fallback: FakeProvider
) -> None:
    reconfirm_pending(store, clock, ctx)

    class Racer:
        def check(self, doc: ProposalDoc, valuation: PriceTable) -> ThesisVerdict:
            current: PortfolioDoc = store.load_portfolio()
            store.save_portfolio(current.model_copy(update={"rev": current.rev + 1}))
            return ThesisVerdict("keep")

    eng = ReconfirmEngine(store, clock, FakeCalendar(), FakeVerifier(), primary, fallback, Racer())
    assert eng.run(NEW, cfg).outcome == "aborted_state_changed"
    assert load(store, NEW).state == "PROPOSED"


# -- tick dispatch -----------------------------------------------------------------------------------------------


class RecordingReconfirm:
    def __init__(self) -> None:
        self.ran: list[str] = []

    def run(self, proposal_id: str, cfg: Config) -> None:
        self.ran.append(proposal_id)


def test_tick_dispatches_the_reconfirm_job(store: FileStore, clock: FixedClock, ctx: CommandContext, cfg: Config) -> None:
    reconfirm_pending(store, clock, ctx)
    rec = RecordingReconfirm()
    assert tick(clock, store, cfg, reconfirm=rec) == 0  # type: ignore[arg-type]
    assert rec.ran == [NEW]


def test_tick_does_not_dispatch_when_paused(store: FileStore, clock: FixedClock, ctx: CommandContext, cfg: Config) -> None:
    reconfirm_pending(store, clock, ctx)
    store.save_portfolio(make_portfolio(status="PAUSED"))
    rec = RecordingReconfirm()
    tick(clock, store, cfg, reconfirm=rec)  # type: ignore[arg-type]
    assert rec.ran == []


def test_tick_end_to_end_validates_then_the_user_can_approve(
    store: FileStore, clock: FixedClock, ctx: CommandContext, cfg: Config, primary: FakeProvider, fallback: FakeProvider
) -> None:
    reconfirm_pending(store, clock, ctx)
    clock.advance(minutes=1)
    assert tick(clock, store, cfg, reconfirm=engine(store, clock, primary, fallback)) == 0
    assert load(store, NEW).state == "VETO_WINDOW"
    assert handle(f"APPROVE {NEW}", cli_sender(clock), ctx).startswith("✅")
    assert load(store, NEW).state == "APPROVED"
