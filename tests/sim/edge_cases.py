"""Edge cases: sleep, late runs, expiry boundaries, pause/resume, unpriceable pairs, DST and holidays (TS §7, NN-14/3x).

The task brief cites ``TS §5 — EDGE-*``; the EDGE rows are TS §7 (§5 is dry-run mode). Not covered because this build has
no code for them: EDGE-15/16 (the sweeper opens every window on its first sweep; there is no per-channel delivery
receipt), EDGE-24/25 (``SET auto_approve`` does not touch open proposals' stored mode) and EDGE-32 (message-id dedupe
lives in the bot).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from stockmanager.commands.auth import Sender
from stockmanager.commands.handlers import CommandContext, handle
from stockmanager.config import Config
from stockmanager.domain.docs import PriceQuote, PriceTable
from stockmanager.notify.deliver import deliver_pending
from stockmanager.notify.outbox import enqueue
from stockmanager.notify.templates import system_ready
from stockmanager.pricing.errors import MixedSourceError
from stockmanager.scheduler.scheduler import ScheduleSlot
from stockmanager.scheduler.sweeper import sweep
from stockmanager.scheduler.tick import tick
from stockmanager.sim import RecordingChannel, StubReasoner
from stockmanager.sim.clock import PT
from tests.support.assertions import (
    alerts,
    assert_audit,
    assert_lockfree,
    assert_logsync,
    assert_nofill,
    assert_recon,
    books,
    proposal_state,
    workspace_hashes,
)
from tests.support.proposals import advance
from tests.support.reasoner_output import hold_output, pair_output
from tests.support.world import (
    EVAL_SAT,
    EVAL_SLOT,
    EXEC,
    EXPIRES,
    P_BASE,
    PAIR_ID,
    SESSION_OPEN,
    SINGLE_ID,
    World,
    pr_pair,
    pr_single,
)

FRI_20 = datetime(2026, 10, 23, 20, 0, tzinfo=PT)
DST_EVAL = datetime(2026, 10, 31, 9, 0, tzinfo=PT)


def cli(w: World, at: datetime | None = None) -> Sender:
    """The trusted owner, as of ``at`` (the message timestamp, TS D11)."""
    return Sender("cli", None, None, at or w.clock())


def ctx(w: World, cfg: Config | None = None) -> CommandContext:
    """A command context over the world's current store."""
    return CommandContext(w.store, cfg or Config(), w.clock, w.calendar, w.yahoo)


# -- machine sleep across an evaluation slot (EDGE-01, EDGE-03) ---------------------------------------------------


def test_edge01_sleep_through_the_slot_runs_one_late_evaluation(world: World) -> None:
    """TS EDGE-01: asleep Fri 20:00 until Sat 11:30. One evaluation, labelled "ran late", not one per missed minute."""
    world.seed()
    world.clock.set(FRI_20)
    reasoner = StubReasoner(hold_output())
    engine = world.evaluation(reasoner)

    world.clock.set(datetime(2026, 10, 24, 11, 30, tzinfo=PT))
    for _ in range(4):  # the cron keeps firing every minute after waking
        assert tick(world.clock, world.store, Config(), evaluation=engine) == 0
        world.clock.advance(minutes=1)

    assert len(reasoner.inputs) == 1
    (alert,) = alerts(world.root, "evaluation_complete")
    assert alert["fields"]["ran_late"] == "True" and "(ran late)" in alert["body"]


def test_edge03_a_week_asleep_gives_one_late_run_not_seven(world: World) -> None:
    """TS EDGE-03: asleep from Sat 10-24 to Sat 10-31 08:00 (an hour before the next slot): exactly one evaluation."""
    world.seed()
    reasoner = StubReasoner(hold_output())
    engine = world.evaluation(reasoner)
    world.clock.set(datetime(2026, 10, 31, 8, 0, tzinfo=PT))

    for _ in range(3):
        tick(world.clock, world.store, Config(), evaluation=engine)
        world.clock.advance(minutes=1)

    assert [i.cycle_id for i in reasoner.inputs] == ["2026-10-24"]


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (datetime(2026, 10, 24, 9, 0, tzinfo=PT), date(2026, 10, 23)),  # EDGE-01: Friday's close
        (datetime(2026, 12, 26, 9, 0, tzinfo=PT), date(2026, 12, 24)),  # EDGE-14: Fri 12-25 closed, Thu 12-24 early close
    ],
)
def test_edge01_edge14_valuation_uses_the_last_completed_session(world: World, at: datetime, expected: date) -> None:
    """TS EDGE-01 / EDGE-14: a Saturday evaluation prices Friday's (or the last real) close. Yahoo has no Saturday bar."""
    world.seed()
    world.calendar.holidays = world.calendar.holidays | {date(2026, 12, 25)}  # CAL-2026-27: Christmas is closed
    world.clock.set(at)
    slot = ScheduleSlot(slot_time=at, cycle_id=at.date().isoformat(), late=False)
    world.evaluation(StubReasoner(hold_output())).run(slot, Config())
    assert {d for _t, d, _k in world.yahoo.calls} == {expected}


# -- late delivery, fill window and expiry (EDGE-04..06, EDGE-17, EDGE-18) ------------------------------------------------


def test_edge04_asleep_at_the_open_a_fill_at_07_10_still_happens(world: World) -> None:
    """TS EDGE-04: the machine wakes at 07:10 (inside the 07:30 expiry); the tick fills both legs once."""
    world.seed()
    world.put(pr_pair())
    world.clock.set(EXEC.replace(minute=10, hour=7))

    tick(world.clock, world.store, Config(), execution=world.engine())

    assert [t.trade_id for t in world.store.load_portfolio().trades][-2:] == [f"{PAIR_ID}-sell1", f"{PAIR_ID}-buy1"]
    assert proposal_state(world.store, PAIR_ID) == "EXECUTED"
    assert_audit(world.store.load_portfolio(), world.store)
    assert_recon(world.store)


@pytest.mark.parametrize("run_at", [(6, 38), (7, 10)])
def test_fill_timestamp_is_session_open(world: World, run_at: tuple[int, int]) -> None:
    """TS PRC-03 / EDGE-04 / A-AUDIT: however late the job runs, the fill time is 06:30 PT and not the run time."""
    world.seed()
    world.put(pr_pair())
    world.clock.set(EXEC.replace(hour=run_at[0], minute=run_at[1]))
    world.engine().run_proposal(PAIR_ID)
    stamps = {t.timestamp for t in world.store.load_portfolio().trades if t.trade_id.startswith(PAIR_ID)}
    assert stamps == {SESSION_OPEN}


def test_edge05_asleep_past_expiry_expires_the_proposal_and_alerts_once(world: World) -> None:
    """TS EDGE-05: waking at 07:31 finds the proposal expired. Nothing fills; one "proposal expired" alert."""
    world.seed()
    world.put(pr_pair())
    before = books(world.store)
    world.clock.set(EXPIRES.replace(minute=31))

    for _ in range(3):
        tick(world.clock, world.store, Config(), execution=world.engine())

    assert proposal_state(world.store, PAIR_ID) == "EXPIRED"
    assert_nofill(before, world.store)
    assert len(alerts(world.root, "proposal_expired")) == 1


def test_edge06_expiry_is_inclusive_at_07_30_00_and_over_at_07_30_01(world: World, tmp_path_factory: pytest.TempPathFactory) -> None:
    """TS EDGE-06 (D10): a run at exactly 07:30:00 fills; a run one second later expires the proposal."""
    world.seed()
    world.put(pr_pair())
    world.clock.set(EXPIRES)
    assert world.engine().run_proposal(PAIR_ID).outcome == "completed"
    assert proposal_state(world.store, PAIR_ID) == "EXECUTED"

    late = World(tmp_path_factory.mktemp("late") / "ws", now=EXPIRES + timedelta(seconds=1))
    late.seed()
    late.put(pr_pair())
    before = books(late.store)
    assert late.engine().run_proposal(PAIR_ID).outcome == "no_op"
    sweep(late.clock(), late.store, late.clock)
    assert proposal_state(late.store, PAIR_ID) == "EXPIRED"
    assert_nofill(before, late.store)


def test_edge07_vetoes_sent_before_a_sleep_are_honoured_when_processed_late(world: World) -> None:
    """TS EDGE-07 / NN-02 (D11): asleep 11:00-13:20. A veto stamped 12:50 and one stamped 13:10 (after the window closed
    at 13:00 but before any fill) are both honoured; nothing fills at the open."""
    world.seed()
    world.put(pr_pair(state="VETO_WINDOW"))
    world.put(pr_single(state="VETO_WINDOW"))
    before = books(world.store)
    world.clock.set(datetime(2026, 10, 24, 13, 20, tzinfo=PT))

    sweep(world.clock(), world.store, world.clock)  # the window has closed: auto-approved first
    assert proposal_state(world.store, PAIR_ID) == "APPROVED"
    inside = handle(f"VETO {PAIR_ID} too tech-heavy", cli(world, datetime(2026, 10, 24, 12, 50, tzinfo=PT)), ctx(world))
    after = handle(f"VETO {SINGLE_ID}", cli(world, datetime(2026, 10, 24, 13, 10, tzinfo=PT)), ctx(world))
    assert inside.startswith("✅") and after.startswith("✅")

    world.clock.set(EXEC)
    tick(world.clock, world.store, Config(), execution=world.engine())
    assert proposal_state(world.store, PAIR_ID) == proposal_state(world.store, SINGLE_ID) == "VETOED"
    assert_nofill(before, world.store)


@pytest.mark.parametrize(
    ("delivered", "state", "cause"),
    [
        (datetime(2026, 10, 24, 19, 0, tzinfo=PT), "VETO_WINDOW", "delivered"),  # deadline 23:00 Sat, before the Monday open
        (datetime(2026, 10, 25, 3, 0, tzinfo=PT), "VETO_WINDOW", "delivered"),  # deadline 07:00 Sun
        (datetime(2026, 10, 26, 5, 0, tzinfo=PT), "EXPIRED", "late_delivery"),  # deadline 09:00 Mon is after the 06:30 open
    ],
)
def test_edge17_late_delivery_is_expired_only_if_the_window_would_pass_the_open(
    world: World, delivered: datetime, state: str, cause: str
) -> None:
    """TS EDGE-17 (D10): an auto-approve proposal whose recomputed veto deadline would pass its execution time is EXPIRED."""
    world.seed()
    world.put(pr_pair(state="PROPOSED"))
    world.clock.set(delivered)

    sweep(delivered, world.store, world.clock)

    doc = world.proposal(PAIR_ID)
    assert (doc.state, doc.transitions[-1].cause) == (state, cause)
    if state == "VETO_WINDOW":
        assert doc.veto_deadline == delivered + timedelta(hours=4)


def test_edge18_veto_window_counts_elapsed_hours_across_the_dst_change(world: World) -> None:
    """TS EDGE-18: 24 h from Sat 2026-10-31 09:00 PDT is 2026-11-01 16:00 UTC (08:00 PST), not 09:00 PST (17:00 UTC)."""
    world.seed()
    monday = datetime(2026, 11, 2, 6, 30, tzinfo=PT)
    world.put(
        pr_pair(state="PROPOSED", created_at=DST_EVAL).model_copy(
            update={
                "veto_window_hours": 24,
                "execute_at": monday,
                "expires_at": monday + timedelta(minutes=60),
                "fill_session_date": monday.date(),
            }
        )
    )
    world.clock.set(DST_EVAL)
    sweep(DST_EVAL, world.store, world.clock)
    deadline = world.proposal(PAIR_ID).veto_deadline
    assert deadline is not None and deadline.astimezone(UTC) == datetime(2026, 11, 1, 16, 0, tzinfo=UTC)


def test_edge13_a_market_holiday_moves_the_fill_and_its_expiry_to_tuesday(tmp_path_factory: pytest.TempPathFactory, fast_fsync: None) -> None:
    """TS EDGE-13: HOLIDAY_EVAL Sat 2027-01-16; Mon 2027-01-18 is closed, so the proposal executes Tue 01-19 06:30, expires 07:30."""
    eval_at = datetime(2027, 1, 16, 9, 0, tzinfo=PT)
    w = World(tmp_path_factory.mktemp("hol") / "ws", now=eval_at, holidays=[date(2027, 1, 18)])
    w.seed()
    slot = ScheduleSlot(slot_time=eval_at, cycle_id="2027-01-16", late=False)
    assert w.evaluation(StubReasoner(pair_output())).run(slot, Config()).outcome == "completed"

    (path,) = (w.root / "proposals").glob("*.json")
    doc = w.proposal(path.stem)
    assert doc.execute_at == datetime(2027, 1, 19, 6, 30, tzinfo=PT)
    assert doc.expires_at == datetime(2027, 1, 19, 7, 30, tzinfo=PT)
    assert doc.fill_session_date == date(2027, 1, 19)


# -- pause and resume (NN-31, NN-33..35, NN-37) ---------------------------------------------------------------------------


def test_nn31_execution_firing_while_paused_does_nothing(world: World) -> None:
    """TS NN-31: the execution job fires at ``EXEC`` against a PAUSED book (cancellation "failed"); no fill, still APPROVED."""
    world.seed(status="PAUSED")
    world.put(pr_pair())
    before = books(world.store)

    assert world.engine().run_proposal(PAIR_ID).outcome == "skipped_not_live"

    assert_nofill(before, world.store)
    assert proposal_state(world.store, PAIR_ID) == "APPROVED"
    assert_lockfree(world.store)


def test_nn31_the_sweeper_converges_a_paused_book_that_still_has_open_proposals(world: World) -> None:
    """TS NN-31 / NN-33 via the dispatcher: status PAUSED but the APPROVED proposal survived (cancellation failed). The next
    tick cancels it before any job could run, and nothing fills."""
    world.seed(status="PAUSED")
    world.put(pr_pair())
    before = books(world.store)

    tick(world.clock, world.store, Config(), execution=world.engine())

    assert proposal_state(world.store, PAIR_ID) == "CANCELLED_PAUSED"
    assert_nofill(before, world.store)


def test_nn33_pause_cancels_every_open_proposal_and_names_them(world: World) -> None:
    """TS NN-33: three open proposals, then PAUSE: all ``CANCELLED_PAUSED``, the reply and the alert list all three IDs."""
    world.seed()
    ids = ["P-2026-10-24-01", "P-2026-10-24-02", "P-2026-10-24-03"]
    world.put(pr_pair(ids[0], state="VETO_WINDOW"))
    world.put(pr_pair(ids[1], state="VETO_WINDOW", approval_mode="explicit"))
    world.put(pr_single(ids[2], state="APPROVED"))

    reply = handle("PAUSE", cli(world), ctx(world))

    assert reply.startswith("✅") and all(i in reply for i in ids)
    assert [proposal_state(world.store, i) for i in ids] == ["CANCELLED_PAUSED"] * 3
    (alert,) = alerts(world.root, "system_pause_resume")
    assert all(i in alert["body"] for i in ids)
    assert world.store.load_portfolio().status == "PAUSED"


def test_nn34_resume_does_not_revive_cancelled_proposals(world: World) -> None:
    """TS NN-34 / E2E-05: PAUSE, RESUME, then the open arrives. Nothing fills; the proposal stays ``CANCELLED_PAUSED``."""
    world.seed()
    world.put(pr_pair())
    before = books(world.store)
    assert handle("PAUSE", cli(world), ctx(world)).startswith("✅")
    assert handle("RESUME", cli(world), ctx(world)).startswith("✅")
    assert world.store.load_portfolio().status == "LIVE"

    world.clock.set(EXEC)
    for _ in range(3):
        tick(world.clock, world.store, Config(), execution=world.engine())
        world.clock.advance(minutes=1)

    assert proposal_state(world.store, PAIR_ID) == "CANCELLED_PAUSED"
    assert_nofill(before, world.store)


def test_nn35_a_paused_system_moves_nothing_through_evaluations_and_a_fill_time(world: World) -> None:
    """TS NN-35 / NN-30: PAUSED across two evaluation slots and an open: no pricing, no Reasoner, books byte-identical."""
    world.seed(status="PAUSED")
    world.put(pr_pair(state="VETOED"))  # a terminal proposal file must stay byte-identical too
    reasoner = StubReasoner(pair_output())
    engine = world.evaluation(reasoner)
    before = workspace_hashes(world.root)

    for when in (EVAL_SAT, EXEC, datetime(2026, 10, 31, 9, 0, tzinfo=PT)):
        world.clock.set(when)
        tick(world.clock, world.store, Config(), execution=world.engine(), evaluation=engine)

    assert workspace_hashes(world.root) == before
    assert world.yahoo.calls == [] and world.finnhub.calls == [] and reasoner.inputs == []


def test_nn37_pause_beats_auto_approve(world: World) -> None:
    """TS NN-37: PAUSE a minute into the window; at window close the sweeper must not approve it."""
    world.seed()
    world.put(pr_pair(state="VETO_WINDOW"))
    world.clock.set(EVAL_SAT + timedelta(minutes=1))
    assert handle("PAUSE", cli(world), ctx(world)).startswith("✅")

    world.clock.set(datetime(2026, 10, 24, 13, 5, tzinfo=PT))
    sweep(world.clock(), world.store, world.clock)

    assert proposal_state(world.store, PAIR_ID) == "CANCELLED_PAUSED"


# -- unpriceable and mixed-source prices (NN-14, EDGE-08, EDGE-12, EDGE-26..29, EDGE-31) --------------------------------------


def test_nn14_edge29_one_unpriceable_leg_means_neither_leg_fills(world: World) -> None:
    """TS NN-14: the sell leg prices on Yahoo, the buy leg on neither. No half pair; one alert naming MSFT."""
    world.seed()
    world.put(pr_pair())
    world.prices("yahoo", MSFT="timeout")
    world.prices("finnhub", MSFT="http_error")
    before = books(world.store)

    assert world.engine().run_proposal(PAIR_ID).outcome == "failed_closed"

    assert_nofill(before, world.store)
    assert proposal_state(world.store, PAIR_ID) == "APPROVED"
    (alert,) = alerts(world.root, "price_source_failure")
    assert "MSFT" in alert["fields"]["tickers"]


def test_edge29_a_pair_priced_on_two_sources_is_retried_whole_on_the_fallback(world: World) -> None:
    """TS EDGE-29: Yahoo prices AAPL but not MSFT. Both legs are re-priced on Finnhub and share one ``price_source``."""
    world.seed()
    world.put(pr_pair())
    world.prices("yahoo", MSFT="timeout")

    assert world.engine().run_proposal(PAIR_ID).outcome == "completed"

    sources = {t.price_source for t in world.store.load_portfolio().trades if t.trade_id.startswith(PAIR_ID)}
    assert sources == {"finnhub"}


def test_edge12_edge28_one_failed_ticker_moves_the_whole_valuation_to_the_fallback(world: World) -> None:
    """TS EDGE-12 / EDGE-26 / EDGE-28: Yahoo fails only SPY (the benchmark); every price in the cycle comes from Finnhub."""
    world.seed()
    world.clock.set(EVAL_SAT)
    world.prices("yahoo", SPY="timeout")
    reasoner = StubReasoner(hold_output())

    assert world.evaluation(reasoner).run(EVAL_SLOT, Config()).outcome == "completed"

    (seen,) = reasoner.inputs
    assert seen.price_source == "finnhub"
    assert all(p.price == str(P_BASE[p.ticker]) for p in seen.portfolio.positions)


def test_edge27_edge08_a_fallback_that_cannot_price_everything_fails_closed(world: World) -> None:
    """TS EDGE-27 / NN-11: Yahoo fails MSFT, Finnhub fails JPM. Prices are not patched together; nothing is written."""
    world.seed()
    world.clock.set(EVAL_SAT)
    world.prices("yahoo", MSFT="timeout")
    world.prices("finnhub", JPM="timeout")
    before = workspace_hashes(world.root)
    reasoner = StubReasoner(pair_output())

    assert world.evaluation(reasoner).run(EVAL_SLOT, Config()).outcome == "failed_closed"

    assert workspace_hashes(world.root) == before
    assert reasoner.inputs == []
    assert not (world.root / "proposals").exists()


def test_edge31_a_mixed_source_price_table_cannot_be_built() -> None:
    """TS EDGE-31: the guard is in the type, below any job: one Finnhub quote in a Yahoo table raises."""
    day = date(2026, 10, 23)
    now = datetime(2026, 10, 23, 14, 0, tzinfo=PT)
    quotes = {
        "AAPL": PriceQuote("AAPL", day, "close", Decimal(200), "yahoo", now),
        "MSFT": PriceQuote("MSFT", day, "close", Decimal(400), "finnhub", now),
    }
    with pytest.raises(MixedSourceError):
        PriceTable(source="yahoo", session_date=day, quotes=quotes)


def test_edge09_prices_ok_news_down_means_no_proposals(world: World) -> None:
    """TS EDGE-09 (D14): valuation and report happen, but no evidence means no proposals that cycle."""
    world.seed()
    world.clock.set(EVAL_SAT)
    engine = world.evaluation(StubReasoner(pair_output()), news_status="unavailable")
    assert engine.run(EVAL_SLOT, Config()).outcome == "completed"
    assert len(alerts(world.root, "news_unavailable")) == 1
    assert not (world.root / "proposals").exists() or not list((world.root / "proposals").glob("*.json"))


# -- the outbox across an outage (EDGE-10, EDGE-11) ---------------------------------------------------------------------------


def test_edge10_edge11_queued_alerts_replay_in_order_with_their_original_timestamps(world: World) -> None:
    """TS EDGE-10 / EDGE-11: three alerts queue while both channels are down; after a restart and recovery at 11:00 they
    are sent once each, in order, stamped with the time they were raised."""
    discord, mainchat = RecordingChannel("discord", world.clock), RecordingChannel("mainchat", world.clock)
    discord.down = mainchat.down = True
    raised = [datetime(2026, 10, 24, 9, m, tzinfo=PT) for m in (10, 12, 15)]
    for n, at in enumerate(raised):
        world.clock.set(at)
        alert = system_ready()
        alert.dedupe_key = f"outage:{n}"
        enqueue(alert, world.store)
        assert deliver_pending(world.store, [discord, mainchat], world.clock) == []  # nothing gets through

    world.restart(0)  # the process restarts; the queue is files, so it survives
    world.clock.set(datetime(2026, 10, 24, 11, 0, tzinfo=PT))
    discord.down = mainchat.down = False
    deliver_pending(world.store, [discord, mainchat], world.clock)

    ids = sorted(a["alert_id"] for a in alerts(world.root))
    for channel in (discord, mainchat):
        assert [n for n, _ in channel.sent] == ids  # in order, no duplicates
    assert [r.embed["timestamp"] for _n, r in discord.sent if r.embed] == [t.isoformat() for t in raised]
    assert list((world.root / "alerts" / "pending").glob("*.json")) == []


# -- expired proposals vs every command (EDGE-21, EDGE-23) -----------------------------------------------------------------------


@pytest.mark.parametrize("command", ["VETO", "APPROVE"])
def test_edge21_veto_and_approve_of_an_expired_proposal_are_refused(world: World, command: str) -> None:
    """TS EDGE-21: ``VETO``/``APPROVE`` on ``EXPIRED`` get ❌ and change nothing (the spec's exact text differs; see below)."""
    world.seed()
    world.put(pr_pair(state="EXPIRED", approval_mode="explicit"))
    before = world.store.proposal_path(PAIR_ID).read_bytes()

    reply = handle(f"{command} {PAIR_ID}", cli(world), ctx(world))

    assert reply.startswith("❌") and "EXPIRED" in reply
    assert world.store.proposal_path(PAIR_ID).read_bytes() == before


def test_edge21_edge23_reconfirm_of_an_expired_proposal_makes_a_new_explicit_one_and_silence_fills_nothing(world: World) -> None:
    """TS EDGE-21 / EDGE-23: ``RECONFIRM`` → ✅ with a new ID, mode ``explicit`` even though auto-approve is on, and the
    original stays voided: executing it fills nothing."""
    world.seed()
    world.put(pr_pair(state="EXPIRED"))
    world.clock.set(EXPIRES.replace(minute=45))
    before = books(world.store)

    reply = handle(f"RECONFIRM {PAIR_ID}", cli(world), ctx(world, Config(auto_approve=True)))

    assert reply.startswith("✅") and "->" in reply
    new_id = reply.split("->")[1].split(".")[0].split()[0]
    new = world.proposal(new_id)
    assert new.approval_mode == "explicit" and new.state == "PROPOSED" and new.reconfirms == PAIR_ID
    assert world.proposal(PAIR_ID).reconfirmed_as == new_id
    assert world.engine().run_proposal(PAIR_ID).outcome == "no_op"
    assert world.engine().run_proposal(new_id).outcome == "no_op"
    assert_nofill(before, world.store)
    assert_logsync(world.store)


def test_advance_helper_builds_a_chain_the_engine_accepts(world: World) -> None:
    """Guard the fixture: ``advance`` + ``pr_pair`` produce a hash chain ``run_proposal`` accepts (so no test above passes by accident)."""
    world.seed()
    doc = advance(pr_pair(state="VETO_WINDOW"), "APPROVED", EVAL_SAT)
    world.put(doc)
    assert world.engine().run_proposal(PAIR_ID).outcome == "completed"
