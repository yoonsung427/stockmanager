"""Behaviour around the former xfail gaps that the original tests do not pin down: the refusals, the edges, the helpers."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from stockmanager.commands.auth import Sender
from stockmanager.commands.handlers import CommandContext, handle
from stockmanager.config import Config
from stockmanager.domain.sessions import last_completed_session
from stockmanager.domain.timeutil import PT, add_elapsed
from stockmanager.sim import FakeCalendar, StubReasoner
from tests.integration.opening_flow import SUNDAY, staged
from tests.support.assertions import alerts, assert_nofill, books, proposal_state
from tests.support.reasoner_output import pair_output
from tests.support.world import EVAL_SAT, EVAL_SLOT, PAIR_ID, World, pr_pair


def command(w: World, text: str, at: datetime) -> str:
    ctx = CommandContext(w.store, Config(), w.clock, w.calendar, w.yahoo)
    return handle(text, Sender("cli", None, None, at), ctx)


# -- APPROVE OPENING ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["APPROVED", "LIVE", "PAUSED", "CLOSED"])
def test_cmd_a04_approve_opening_outside_staged_is_refused(world: World, status: str) -> None:
    """TS CMD-A04: only a STAGED book has an opening to approve."""
    staged(world, status)
    before = books(world.store)
    reply = command(world, "APPROVE OPENING", SUNDAY)
    assert reply.startswith("❌") and f"status {status}" in reply
    assert world.store.load_portfolio().status == status
    assert_nofill(before, world.store)


def test_cmd_a08_approve_opening_without_picks_is_refused(world: World) -> None:
    """TS CMD-A08."""
    doc = staged(world, "STAGED")
    world.store.save_portfolio(doc.model_copy(update={"config": {"staged_orders": []}}))
    reply = command(world, "approve opening", SUNDAY)
    assert reply == "❌ No staged picks" and world.store.load_portfolio().status == "STAGED"


def test_cmd_a03_the_reply_names_the_next_open(world: World) -> None:
    staged(world, "STAGED")
    assert "Mon 2026-10-26 06:30 PT" in command(world, "APPROVE OPENING", SUNDAY)


# -- CLOSE CONFIRM ----------------------------------------------------------------------------------------------------------


def test_close_confirm_queues_one_final_report_alert(world: World) -> None:
    world.seed()
    world.clock.set(EVAL_SAT.replace(day=22, hour=14))
    now = world.clock()
    assert command(world, "CLOSE", now).startswith("✅")
    assert command(world, "CLOSE CONFIRM", now + timedelta(minutes=1)).startswith("✅")
    (report,) = alerts(world.root, "final_report")
    assert report["fields"]["source"] == "yahoo" and report["fields"]["ending_value"]


def test_close_confirm_that_cannot_price_changes_nothing(world: World) -> None:
    world.seed()
    world.clock.set(EVAL_SAT.replace(day=22, hour=14))
    world.prices("yahoo", AAPL="timeout")
    world.prices("finnhub", AAPL="timeout")
    now = world.clock()
    command(world, "CLOSE", now)
    reply = command(world, "CLOSE CONFIRM", now + timedelta(minutes=1))
    assert reply.startswith("❌") and "retry" in reply
    assert world.store.load_portfolio().status == "LIVE"
    assert (world.root / "state" / "pending_close.json").exists()


# -- evaluation: orphans, news, new names ---------------------------------------------------------------------------------------------


def test_a_leftover_orphan_is_closed_out_when_the_retry_proposes_less(world: World) -> None:
    """Two never-announced proposals from a crashed run; the retry builds one. The spare cannot be deleted, so it ends."""
    world.seed()
    world.clock.set(EVAL_SAT)
    spare_id = "P-2026-10-24-02"
    world.put(pr_pair(PAIR_ID, state="PROPOSED"))
    world.put(pr_pair(spare_id, state="PROPOSED"))

    assert world.evaluation(StubReasoner(pair_output())).run(EVAL_SLOT, Config()).outcome == "completed"

    assert proposal_state(world.store, PAIR_ID) == "PROPOSED"  # overwritten by the retry's own proposal
    spare = world.proposal(spare_id)
    assert spare.state == "VETOED" and "superseded" in spare.transitions[-1].cause
    assert spare.transitions[-1].actor == "system:evaluation"


def test_news_down_reports_the_reason_and_never_asks_the_reasoner(world: World) -> None:
    world.seed()
    world.clock.set(EVAL_SAT)
    reasoner = StubReasoner(pair_output())
    result = world.evaluation(reasoner, news_status="unavailable").run(EVAL_SLOT, Config())
    assert (result.outcome, result.detail) == ("completed", "news_unavailable")
    assert reasoner.inputs == []


def test_an_announced_proposal_is_not_replaced_by_a_rerun(world: World) -> None:
    """Once the owner has the ``veto_window_open`` alert the proposal is theirs: a forced re-run adds beside it."""
    world.seed()
    world.clock.set(EVAL_SAT)
    reasoner = StubReasoner(pair_output())
    assert world.evaluation(reasoner).run(EVAL_SLOT, Config()).outcome == "completed"
    assert world.evaluation(reasoner).run(EVAL_SLOT, Config()).outcome == "completed"
    assert len(list((world.root / "proposals").glob("*.json"))) == 2
    assert proposal_state(world.store, PAIR_ID) == "PROPOSED"


# -- helpers ---------------------------------------------------------------------------------------------------------------


def test_add_elapsed_counts_real_hours_across_the_fall_back() -> None:
    start = datetime(2026, 10, 31, 9, 0, tzinfo=PT)
    assert add_elapsed(start, timedelta(hours=24)) == datetime(2026, 11, 1, 8, 0, tzinfo=PT)
    assert (start + timedelta(hours=24)).hour == 9  # the wall-clock sum this replaces
    assert add_elapsed(start, timedelta(hours=24)).tzinfo is PT


def test_add_elapsed_refuses_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        add_elapsed(datetime(2026, 10, 31, 9, 0), timedelta(hours=1))


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 24, 9, 0, tzinfo=PT), date(2026, 10, 23)),  # Saturday -> Friday
        (datetime(2026, 10, 26, 9, 0, tzinfo=PT), date(2026, 10, 23)),  # Monday before the close -> Friday
        (datetime(2026, 10, 26, 13, 19, tzinfo=PT), date(2026, 10, 23)),  # inside the settle margin
        (datetime(2026, 10, 26, 13, 20, tzinfo=PT), date(2026, 10, 26)),  # settled
    ],
)
def test_last_completed_session(now: datetime, expected: date) -> None:
    assert last_completed_session(FakeCalendar(), now) == expected


def test_last_completed_session_skips_holidays() -> None:
    calendar = FakeCalendar([date(2026, 11, 26)])
    assert last_completed_session(calendar, datetime(2026, 11, 27, 9, 0, tzinfo=PT)) == date(2026, 11, 25)
