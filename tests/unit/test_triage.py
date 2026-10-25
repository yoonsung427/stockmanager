"""Triage skeleton, log-only shadow mode (v2 design D1/R7; test plan §2 A2 gate, §3 A1-A5, R4/R5 fault cases)."""

from __future__ import annotations

import ast
import fcntl
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from stockmanager.cli import build_parser
from stockmanager.config.model import BLOCKED_SET_PREFIXES, SETTABLE_KEYS, Config
from stockmanager.config.triage import TriageConfig
from stockmanager.domain.docs import PortfolioDoc
from stockmanager.scheduler import state as scheduler_state_mod
from stockmanager.scheduler.scheduler import DueJob, ScheduleSlot, list_all_jobs, list_due_jobs
from stockmanager.scheduler.state import SchedulerState
from stockmanager.scheduler.tick import TICK_LOCK_NAME, tick
from stockmanager.sim import FakeClock, FakePidChecker
from stockmanager.sim.clock import PT
from stockmanager.sim.faults import sqlite_down
from stockmanager.store import FileStore, eventsdb
from stockmanager.triage import (
    Classification,
    Decision,
    EventKind,
    TriageItem,
    TriagePipeline,
    TriageWake,
    WakeQueue,
    prefilter,
)
from stockmanager.triage.decision_log import DecisionLog
from stockmanager.triage.models import ESCALATING_ACTIONS, has_citation
from stockmanager.triage.pipeline import dedupe_key
from stockmanager.triage.prefilter import _PATTERNS
from stockmanager.triage.quiet import QuietHours
from stockmanager.triage.runner import TRIAGE_LOCK_NAME, load_done, read_fetch_log, run_triage, triage_lock
from stockmanager.triage.wakes import CYCLE_PREFIX
from tests.sim.triage_model import FixedTriageModel
from tests.support.world import World
from tests.unit.test_import_rules import PKG, SRC, _imports, _modules, _under

NOON = datetime(2026, 10, 14, 12, 0, tzinfo=PT)  # a Wednesday, outside quiet hours
LIVE = TriageConfig(enabled=True, shadow_mode=False, benchmark_passed=True)
SHADOW = TriageConfig(enabled=True)  # shadow_mode defaults to True
HIGH = Classification(EventKind.EARNINGS, 0.95)
LOW = Classification(EventKind.EARNINGS, 0.40)
HEADLINE = "AAPL reports record earnings, raises full-year guidance"
URL = "https://news.example.com/aapl-earnings"


def item(n: int = 1, *, title: str = HEADLINE, url: str | None = None, at: datetime = NOON) -> TriageItem:
    return TriageItem(f"hash-{n}", URL if url is None else url, title, "", at)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOON)


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> FileStore:
    return FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)


def pipeline(store: FileStore, model: FixedTriageModel, cfg: TriageConfig = LIVE) -> TriagePipeline:
    return TriagePipeline(store, cfg, model, {"AAPL", "MSFT"})


def actions(store_or_run: Any) -> list[str]:
    return [d.action for d in store_or_run.decisions]


# -- pre-filter (A1: pure, no LLM call) ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "passes"),
    [
        (HEADLINE, True),  # held ticker + event kind
        ("AAPL shares were flat on Tuesday", False),  # held ticker, no event hint
        ("Tesla reports record earnings", False),  # event hint, no held ticker
        ("Analyst downgrades $MSFT on cloud worries", True),  # cashtag + rating hint
        ("aapl reports record earnings", False),  # tickers are case-sensitive
        ("AAPLX reports record earnings", False),  # no substring matches
        ("Why Apple (AAPL) stock fell after the SEC subpoena", True),
    ],
)
def test_prefilter_requires_held_ticker_and_event_kind(title: str, passes: bool) -> None:  # A1
    result = prefilter(item(title=title), {"AAPL", "MSFT"})
    assert result.passed is passes
    assert all(isinstance(k, EventKind) for k in result.kinds)


def test_prefilter_single_letter_ticker_needs_cashtag() -> None:  # A1
    assert not prefilter(item(title="A new CEO steps down at the firm"), {"A"}).passed
    assert prefilter(item(title="$A CEO steps down"), {"A"}).passed


def test_prefilter_event_kind_enum_is_closed() -> None:  # A1
    assert set(_PATTERNS) == set(EventKind)
    assert prefilter(item(title="AAPL announces buyback and acquires a startup"), {"AAPL"}).kinds == (
        EventKind.MERGER_ACQUISITION,
        EventKind.CAPITAL_RETURN,
    )


def test_prefilter_is_pure_and_makes_no_llm_call(store: FileStore) -> None:  # A1
    tree = ast.parse((SRC / PKG / "triage" / "prefilter.py").read_text(encoding="utf-8"))
    imported = {n for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) for n in _names(node)}
    assert not {n for n in imported if n.split(".")[0] in {"httpx", "anthropic", "openai", "sqlite3", "socket", "os"}}
    assert not [n for n in imported if _under(n, f"{PKG}.llm") or _under(n, f"{PKG}.adapters") or _under(n, f"{PKG}.store")]
    # and through the pipeline: items failing the hard gate never reach the classifier
    model = FixedTriageModel(HIGH)
    run = pipeline(store, model).run(
        [item(1, title="Tesla reports record earnings"), item(2, title="AAPL shares were flat"), item(3, title="weather")],
        NOON,
    )
    assert model.calls == []
    assert actions(run) == ["silent"]  # only the held-ticker/no-kind item is logged; unrelated news is not
    assert run.decisions[0].hard_gate is False


def _names(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    return [node.module or ""]


# -- confidence gate (A1) --------------------------------------------------------------------------------------------


def test_below_threshold_is_silent_above_wakes(store: FileStore) -> None:  # A1
    model = FixedTriageModel(outputs={"hash-1": LOW, "hash-2": HIGH})
    run = pipeline(store, model).run([item(1), item(2, title="MSFT announces acquisition of a rival")], NOON)
    assert actions(run) == ["silent", "escalate"]
    assert [w.ticker for w in WakeQueue(store.root).all()] == ["MSFT"]


def test_threshold_is_inclusive(store: FileStore) -> None:  # A1
    exactly = Classification(EventKind.EARNINGS, LIVE.confidence_threshold)
    assert actions(pipeline(store, FixedTriageModel(exactly)).run([item()], NOON)) == ["escalate"]


def test_classification_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValueError):
        Classification(EventKind.EARNINGS, 1.5)


# -- dedupe (A4) -----------------------------------------------------------------------------------------------------


def test_duplicate_event_hash_inside_window_escalates_once(store: FileStore) -> None:  # A4
    model = FixedTriageModel(HIGH)
    first = pipeline(store, model).run([item(1)], NOON)
    # the same headline re-syndicated under another URL, 2h later
    second = pipeline(store, model).run([item(2, url="https://other.example.org/x")], NOON + timedelta(hours=2))
    assert actions(first) == ["escalate"] and actions(second) == ["suppressed_duplicate"]
    assert len(WakeQueue(store.root).all()) == 1
    assert first.decisions[0].event_hash == second.decisions[0].event_hash


def test_same_event_is_allowed_again_after_the_window(store: FileStore) -> None:  # A4
    model = FixedTriageModel(HIGH)
    pipeline(store, model).run([item(1)], NOON)
    later = NOON + timedelta(hours=LIVE.dedupe_window_hours, minutes=1)
    assert actions(pipeline(store, model).run([item(2)], later)) == ["escalate"]


def test_classifier_event_id_dedupes_differently_worded_stories(store: FileStore) -> None:  # A4
    same = Classification(EventKind.MERGER_ACQUISITION, 0.99, event_id="aapl-acquires-x")
    model = FixedTriageModel(outputs={"hash-1": same, "hash-2": same})
    run = pipeline(store, model).run(
        [item(1, title="AAPL to acquire X"), item(2, title="AAPL buys startup X in takeover")], NOON
    )
    assert actions(run) == ["escalate", "suppressed_duplicate"]


def test_dedupe_uses_the_atomic_file_store_not_sqlite(store: FileStore) -> None:  # R4
    pipeline(store, FixedTriageModel(HIGH)).run([item()], NOON)
    keys = store.dedupe_keys()
    assert len(keys) == 1 and next(iter(keys)).startswith("triage:AAPL:")
    assert dedupe_key("AAPL", "x") == "triage:AAPL:x"


def test_daily_cap(store: FileStore) -> None:
    cfg = TriageConfig(enabled=True, shadow_mode=False, benchmark_passed=True, max_escalations_per_day=1)
    run = pipeline(store, FixedTriageModel(HIGH), cfg).run(
        [item(1), item(2, title="MSFT announces acquisition of a rival")], NOON
    )
    assert actions(run) == ["escalate", "suppressed_cap"]


# -- quiet hours (R5) ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hhmm", "quiet"),
    [("21:59", False), ("22:00", True), ("03:00", True), ("06:29", True), ("06:30", False), ("12:00", False)],
)
def test_quiet_hours_window_wraps_midnight(hhmm: str, quiet: bool) -> None:
    h, m = map(int, hhmm.split(":"))
    assert QuietHours.parse("22:00-06:30").contains(NOON.replace(hour=h, minute=m)) is quiet


def test_quiet_hours_defer_to_morning_and_window_starts_at_delivery(store: FileStore) -> None:  # R5
    night = datetime(2026, 10, 14, 23, 15, tzinfo=PT)
    run = pipeline(store, FixedTriageModel(HIGH)).run([item()], night)
    (decision,) = run.decisions
    morning = datetime(2026, 10, 15, 6, 30, tzinfo=PT)
    assert decision.action == "defer_digest" and decision.due_at == morning
    (wake,) = WakeQueue(store.root).all()
    assert wake.due_at == morning  # the wake is the delivery; the veto window cannot start before it
    wake_state = _with_wakes(wake)
    for t in (night, datetime(2026, 10, 15, 3, 0, tzinfo=PT), morning - timedelta(seconds=1)):  # nothing pages overnight
        assert _triage_jobs(t, wake_state) == []
    (job,) = _triage_jobs(morning, wake_state)
    assert job.slot is not None and job.slot.slot_time == morning


def test_daytime_item_escalates_immediately(store: FileStore) -> None:
    run = pipeline(store, FixedTriageModel(HIGH)).run([item()], NOON)
    assert run.decisions[0].action == "escalate" and run.decisions[0].due_at == NOON


# -- citation (A5) ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["", "   ", "javascript:alert(1)", "not a url", "https://"])
def test_no_citation_input_cannot_escalate(store: FileStore, url: str) -> None:  # A5 negative
    run = pipeline(store, FixedTriageModel(HIGH)).run([item(url=url)], NOON)
    assert actions(run) == ["blocked_no_citation"]
    assert run.decisions[0].source_url is None
    assert WakeQueue(store.root).all() == [] and store.dedupe_keys() == set()  # nothing claimed, nothing woken


@pytest.mark.parametrize("action", sorted(ESCALATING_ACTIONS | {"shadow_escalate"}))
def test_decision_type_refuses_an_uncited_escalation(action: str) -> None:  # A5: enforced in code
    with pytest.raises(ValueError, match="no citation"):
        Decision(NOON, "AAPL", "h", True, 0.99, action, None, NOON)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no citation"):
        Decision(NOON, "AAPL", "h", True, 0.99, action, "ftp://x/y", NOON)  # type: ignore[arg-type]


def test_database_also_refuses_an_uncited_escalation(store: FileStore) -> None:  # A5: belt and braces
    import sqlite3

    with eventsdb.connect(store.root) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO triage_decisions (ts, ticker, event_hash, hard_gate, confidence, action, source_url)"
            " VALUES ('t', 'AAPL', 'h', 1, 0.9, 'escalate', NULL)"
        )


def test_every_escalation_row_has_a_source_url(store: FileStore) -> None:  # A5
    model = FixedTriageModel(HIGH)
    pipeline(store, model).run([item(1), item(2, url=""), item(3, title="MSFT acquires a rival")], NOON)
    rows = DecisionLog(store.root).read()
    escalations = [r for r in rows if r[5] in ESCALATING_ACTIONS]
    assert len(escalations) == 2 and all(r[6] and has_citation(r[6]) for r in escalations)
    assert [r[5] for r in rows if r[6] is None] == ["blocked_no_citation"]


def test_decision_row_shape(store: FileStore) -> None:  # D7
    pipeline(store, FixedTriageModel(LOW)).run([item()], NOON)
    ((ts, ticker, ev, gate, conf, action, url),) = DecisionLog(store.root).read()
    assert (ts, ticker, gate, conf, action, url) == (NOON.isoformat(), "AAPL", 1, 0.40, "silent", URL)
    assert len(ev) == 64


# -- classifier down (R4) --------------------------------------------------------------------------------------------


def test_classifier_down_fails_closed_and_stops_calling_the_model(store: FileStore) -> None:  # R4
    model = FixedTriageModel(HIGH)
    model.down = True
    run = pipeline(store, model).run([item(1), item(2, title="MSFT announces acquisition of a rival")], NOON)
    assert set(actions(run)) == {"classifier_down"} and not (ESCALATING_ACTIONS & set(actions(run)))
    assert len(model.calls) == 1  # the second item did not wait on a dead model
    assert run.stalled == {"hash-1", "hash-2"}
    assert WakeQueue(store.root).all() == [] and store.dedupe_keys() == set()
    assert DecisionLog(store.root).read() == []  # an outage is not a decision


def test_backlog_reruns_on_recovery_without_double_escalation(store: FileStore, clock: FakeClock) -> None:  # R4
    _write_fetch_log(store, [item(1), item(2, title="MSFT announces acquisition of a rival")])
    held = {"AAPL", "MSFT"}
    model = FixedTriageModel(HIGH)
    model.down = True
    assert run_triage(store, LIVE, model, held) == {"classifier_down": 2}
    assert load_done(store.root) == []  # backlog retained
    model.down = False
    assert run_triage(store, LIVE, model, held) == {"escalate": 2}
    assert len(WakeQueue(store.root).all()) == 2
    assert run_triage(store, LIVE, model, held) == {}  # cursor: nothing left
    # even with the cursor lost, dedupe (checked before dispatch) stops a second escalation
    (store.root / "state" / "triage_done.json").unlink()
    assert run_triage(store, LIVE, model, held) == {"suppressed_duplicate": 2}
    assert len(WakeQueue(store.root).all()) == 2


def test_partial_outage_mid_batch_does_not_double_escalate(store: FileStore) -> None:  # R4
    items = [item(1, title="AAPL beats estimates on earnings"), item(2, title="MSFT announces acquisition")]
    model = FixedTriageModel(HIGH)
    pl = pipeline(store, model)
    assert actions(pl.run(items[:1], NOON)) == ["escalate"]
    model.down = True
    run = pl.run(items, NOON)  # item 1 is replayed (e.g. cursor lost) while the model is down
    assert "escalate" not in actions(run)
    model.down = False
    assert actions(pl.run(items, NOON)) == ["suppressed_duplicate", "escalate"]
    assert len(WakeQueue(store.root).all()) == 2


def test_dedupe_survives_sqlite_down(store: FileStore) -> None:  # R4
    model = FixedTriageModel(HIGH)
    with sqlite_down():
        first = pipeline(store, model).run([item(1)], NOON)
        again = pipeline(store, model).run([item(2)], NOON)
    assert actions(first) == ["escalate"] and actions(again) == ["suppressed_duplicate"]
    assert len(WakeQueue(store.root).all()) == 1  # escalation bookkeeping never needed SQLite
    assert DecisionLog(store.root).read() == []  # the log itself was unavailable
    # after recovery the dedupe key is still honoured
    assert actions(pipeline(store, model).run([item(3)], NOON)) == ["suppressed_duplicate"]


def test_decision_log_reports_unavailable_instead_of_raising(store: FileStore) -> None:  # D7
    d = Decision(NOON, "AAPL", "h", True, 0.1, "silent", URL)
    with sqlite_down():
        assert DecisionLog(store.root).record([d]) is False
    assert DecisionLog(store.root).record([d]) is True


# -- shadow mode (R1 consequence) ------------------------------------------------------------------------------------


def test_shadow_mode_records_decisions_but_never_escalates(store: FileStore) -> None:  # R1
    model = FixedTriageModel(HIGH)
    run = pipeline(store, model, SHADOW).run(
        [item(1), item(2, title="MSFT announces acquisition"), item(3, title="AAPL flat"), item(4, url="")],
        datetime(2026, 10, 14, 23, 0, tzinfo=PT),  # quiet hours too: still no deferral path
    )
    assert actions(run) == ["shadow_escalate", "shadow_escalate", "silent", "blocked_no_citation"]
    assert not ESCALATING_ACTIONS & set(actions(run))
    assert {r[5] for r in DecisionLog(store.root).read()} == {"shadow_escalate", "silent", "blocked_no_citation"}
    assert WakeQueue(store.root).all() == []
    assert store.dedupe_keys() == set()  # shadow-era keys must not pre-empt the later production run
    assert not (store.root / "triage-wakes").exists()
    state = SchedulerState()
    assert not [j for j in list_due_jobs(NOON, Config(), "LIVE", [], state) if j.kind == "triage_escalation"]


@pytest.mark.parametrize(
    ("enabled", "shadow", "benchmark", "live"),
    [(True, False, True, True), (True, True, True, False), (True, False, False, False), (False, False, True, False)],
)
def test_production_enable_needs_all_three_flags(
    store: FileStore, enabled: bool, shadow: bool, benchmark: bool, live: bool
) -> None:  # R1
    cfg = TriageConfig(enabled=enabled, shadow_mode=shadow, benchmark_passed=benchmark)
    assert cfg.escalation_live is live
    run = pipeline(store, FixedTriageModel(HIGH), cfg).run([item()], NOON)
    assert actions(run) == (["escalate"] if live else ["shadow_escalate"])


def test_triage_job_is_a_no_op_when_disabled(store: FileStore) -> None:
    _write_fetch_log(store, [item()])
    assert run_triage(store, TriageConfig(), FixedTriageModel(HIGH), {"AAPL"}) == {"disabled": 1}
    assert not eventsdb.db_path(store.root).exists()  # disabled means untouched, not just silent


def test_strict_defaults_and_file_edit_only() -> None:  # A3, R1
    cfg = Config().triage
    assert (cfg.enabled, cfg.shadow_mode, cfg.benchmark_passed, cfg.escalation_live) == (False, True, False, False)
    assert "triage." in BLOCKED_SET_PREFIXES and not any(k.startswith("triage") for k in SETTABLE_KEYS)
    with pytest.raises(ValidationError):
        Config.model_validate({"triage": {"auto_escalate": True}})
    with pytest.raises(ValidationError):
        TriageConfig(dedupe_window_hours=48)  # the ledger is shared with the outbox's 24h pruning


# -- scheduler: DueJob kind, cycle ids, tick dispatch (R7) -----------------------------------------------------------


def _wake(due: datetime, ticker: str = "AAPL", eh: str = "abcdef0123456789") -> TriageWake:
    from stockmanager.triage.wakes import cycle_id_for

    return TriageWake(cycle_id_for(due, ticker, eh), ticker, eh, URL, due, due)


WEEKLY_DONE = SchedulerState(anchor_date=date(2026, 10, 10), completed_slots=(date(2026, 10, 10),))


def _with_wakes(*wakes: TriageWake, base: SchedulerState = WEEKLY_DONE) -> SchedulerState:
    """A state whose current weekly slot (Sat 2026-10-10) is already complete, so only triage jobs can be due."""
    return base.with_triage_wakes(wakes)


def _triage_jobs(now: datetime, state: SchedulerState) -> list[DueJob]:
    return [j for j in list_due_jobs(now, Config(), "LIVE", [], state) if j.kind == "triage_escalation"]


def test_triage_cycle_id_cannot_collide_with_a_date_slot() -> None:  # R7
    w = _wake(NOON)
    assert w.cycle_id.startswith(CYCLE_PREFIX)
    with pytest.raises(ValueError):
        date.fromisoformat(w.cycle_id)
    with pytest.raises(ValueError):
        date.fromisoformat(w.cycle_id[:10])


def test_list_due_jobs_stays_pure_and_gates_on_due_time_done_and_status() -> None:  # R7
    w = _wake(NOON)
    state = _with_wakes(w)
    before = repr(state)
    assert _triage_jobs(NOON - timedelta(seconds=1), state) == []
    (job,) = _triage_jobs(NOON, state)
    assert job.slot == ScheduleSlot(NOON, w.cycle_id, False) and job.proposal_id is None
    assert _triage_jobs(NOON, state.with_triage_done(w.cycle_id)) == []
    assert repr(state) == before  # nothing mutated, nothing read from disk
    assert list_due_jobs(NOON, Config(), "PAUSED", [], state) == []  # paused never escalates
    assert [j.kind for j in list_all_jobs(NOON, Config(), "LIVE", [], state)] == ["evaluation", "triage_escalation"]


def test_burst_of_wakes_drains_one_per_tick() -> None:  # R7
    a, b = _wake(NOON, "AAPL"), _wake(NOON + timedelta(minutes=1), "MSFT")
    state = _with_wakes(b, a)
    now = NOON + timedelta(minutes=2)
    assert [j.slot.cycle_id for j in _triage_jobs(now, state) if j.slot] == [a.cycle_id]
    assert [j.slot.cycle_id for j in _triage_jobs(now, state.with_triage_done(a.cycle_id)) if j.slot] == [b.cycle_id]


def test_off_cycle_wake_does_not_collide_with_weekly_slot() -> None:  # A2/R7
    cfg = Config()  # weekly, saturday 09:00
    sat = datetime(2026, 10, 17, 9, 30, tzinfo=PT)
    wake = _wake(sat - timedelta(hours=1))
    state = SchedulerState(triage_wakes=(wake,))  # no weekly slot completed yet: Saturday's is due
    jobs = list_due_jobs(sat, cfg, "LIVE", [], state)
    assert [j.kind for j in jobs] == ["evaluation", "triage_escalation"]  # the weekly slot is first, untouched
    assert jobs[0].slot is not None and jobs[0].slot.cycle_id == "2026-10-17"
    assert jobs[1].slot is not None and jobs[1].slot.cycle_id != jobs[0].slot.cycle_id
    # running the wake neither completes nor suppresses the weekly slot
    after_wake = state.with_triage_done(wake.cycle_id)
    assert after_wake.completed_slots == ()
    assert [j.kind for j in list_due_jobs(sat, cfg, "LIVE", [], after_wake)] == ["evaluation"]
    # and a wake that fires on a day with no weekly slot leaves the Saturday slot due later
    wed = list_due_jobs(NOON, cfg, "LIVE", [], _with_wakes(_wake(NOON)))
    assert [j.kind for j in wed] == ["triage_escalation"]
    assert [j.kind for j in list_due_jobs(sat, cfg, "LIVE", [], WEEKLY_DONE.with_triage_done(wed[0].slot.cycle_id))] == ["evaluation"]  # type: ignore[union-attr]


def test_scheduler_state_persists_triage_done_but_not_the_wake_cache() -> None:  # R7
    s = SchedulerState(triage_wakes=(_wake(NOON),)).with_triage_done("triage-x")
    loaded = SchedulerState.from_json(s.to_json())
    assert loaded.triage_done == ("triage-x",) and loaded.triage_wakes == ()
    assert SchedulerState.from_json('{"anchor_date": null}').triage_done == ()  # a pre-triage file still loads
    capped = SchedulerState()
    for i in range(100):
        capped = capped.with_triage_done(f"triage-{i}")
    assert len(capped.triage_done) == 64 and capped.triage_done[-1] == "triage-99"


def _seed_live(store: FileStore) -> None:
    store.save_portfolio(PortfolioDoc(start_cash=Decimal(10000), cash=Decimal(10000), status="LIVE"))


def test_tick_dispatches_due_wake_and_marks_it_done(store: FileStore, clock: FakeClock) -> None:  # R7
    _seed_live(store)
    wake = _wake(clock.now - timedelta(minutes=1))
    WakeQueue(store.root).enqueue(wake)
    seen: list[ScheduleSlot] = []

    def handler(slot: ScheduleSlot) -> bool:
        seen.append(slot)
        return True

    assert tick(clock, store, Config(), triage_escalation=handler) == 0
    assert [s.cycle_id for s in seen] == [wake.cycle_id]
    state = scheduler_state_mod.load(store.root)
    assert state.triage_done == (wake.cycle_id,)
    assert state.completed_slots == (date(2026, 10, 10),)  # only the weekly slot, set by the tick's own evaluation branch
    tick(clock, store, Config(), triage_escalation=handler)
    assert len(seen) == 1  # done is done


def test_tick_leaves_wake_pending_when_unbound_failing_or_not_done(store: FileStore, clock: FakeClock) -> None:  # R7
    _seed_live(store)
    wake = _wake(clock.now)
    WakeQueue(store.root).enqueue(wake)
    tick(clock, store, Config())  # no handler bound
    assert scheduler_state_mod.load(store.root).triage_done == ()

    def boom(_slot: ScheduleSlot) -> bool:
        raise RuntimeError("off-cycle run failed")

    assert tick(clock, store, Config(), triage_escalation=boom) == 0  # a failing wake never fails the tick
    assert scheduler_state_mod.load(store.root).triage_done == ()
    assert tick(clock, store, Config(), triage_escalation=lambda _s: False) == 0
    assert scheduler_state_mod.load(store.root).triage_done == ()


def test_wake_does_not_starve_deliver_pending(store: FileStore, clock: FakeClock) -> None:  # R7
    from stockmanager.notify.deliver import Receipt
    from stockmanager.notify.outbox import Alert, ChannelState, prepare

    _seed_live(store)
    WakeQueue(store.root).enqueue(_wake(clock.now))
    alert = prepare(Alert("x", "info", "t", "b", {}, [], False, "k", {}, channels={"mainchat": ChannelState()}), clock.now)
    (store.root / "alerts" / "pending").mkdir(parents=True, exist_ok=True)
    (store.root / "alerts" / "pending" / f"{alert.alert_id}.json").write_text(alert.to_json())
    order: list[str] = []

    class Chan:
        name = "mainchat"

        def send(self, rendered: Any, *, nonce: str) -> Receipt:
            order.append("deliver")
            return Receipt(channel="mainchat", external_id=nonce, sent_at=clock.now)

    def handler(_slot: ScheduleSlot) -> bool:
        order.append("triage")
        return True

    tick(clock, store, Config(), channels=(Chan(),), triage_escalation=handler)  # type: ignore[arg-type]
    assert order == ["deliver", "triage"]


def test_triage_cron_is_independent_of_the_tick_lock(store: FileStore) -> None:  # R7
    _write_fetch_log(store, [item()])
    tick_lock_path = store.root / "state" / TICK_LOCK_NAME
    tick_lock_path.parent.mkdir(parents=True, exist_ok=True)
    with tick_lock_path.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a long tick is running
        assert run_triage(store, SHADOW, FixedTriageModel(HIGH), {"AAPL"}) == {"shadow_escalate": 1}
    with triage_lock(store.root) as first, triage_lock(store.root) as second:  # single-flight for triage itself
        assert (first, second) == (True, False)
        assert run_triage(store, SHADOW, FixedTriageModel(HIGH), {"AAPL"}) == {"skipped_busy": 1}
    assert (store.root / "state" / TRIAGE_LOCK_NAME).exists()


def test_cron_registration_follows_existing_conventions() -> None:  # R7
    root = SRC.parent
    cron = (root / "ops" / "crontab.example").read_text(encoding="utf-8")
    lines = [ln for ln in cron.splitlines() if ln and not ln.startswith("#")]
    assert any(" -m stockmanager triage " in ln for ln in lines) and any(" -m stockmanager tick " in ln for ln in lines)
    assert not list((root / "ops").glob("*triage*"))  # no launchd plist
    assert build_parser().parse_args(["triage"]).func.__name__ == "cmd_triage"


def test_fetch_log_reader_skips_malformed_rows(store: FileStore) -> None:  # D8
    d = store.root / "fetch-log"
    d.mkdir(parents=True)
    good = {"item_hash": "h", "url": URL, "raw_title": "SECRETTITLE", "raw_snippet": "s", "fetched_at": NOON.isoformat(), "source": "x"}
    (d / "c.jsonl").write_text(json.dumps(good) + "\nnot json\n" + json.dumps({"item_hash": "only"}) + "\n")
    (read,) = read_fetch_log(store.root)
    assert read.item_hash == "h" and "SECRETTITLE" not in repr(read)  # raw text redacted from repr


def _write_fetch_log(store: FileStore, items: list[TriageItem]) -> None:
    rows = [
        {
            "item_hash": i.item_hash,
            "url": i.url,
            "raw_title": i.title,
            "raw_snippet": i.snippet,
            "fetched_at": i.fetched_at.isoformat(),
            "source": "t",
        }
        for i in items
    ]
    assert store.save_fetch_log("2026-10-10", rows)


# -- A2 hard gate: no path from triage to the execution engine ------------------------------------------------------


EXECUTION = f"{PKG}.execution"


def _runtime_import_graph() -> dict[str, set[str]]:
    """module -> project modules it imports at runtime (importing ``a.b.c`` also runs ``a`` and ``a.b``)."""
    known = {m for pkg in _top_level_packages() for m, _ in _modules(pkg)}
    graph: dict[str, set[str]] = {}
    for pkg in _top_level_packages():
        for module, path in _modules(pkg):
            deps: set[str] = set()
            for name, _line in _imports(path, module, runtime_only=True):
                if not _under(name, PKG):
                    continue
                parts = name.split(".")
                for i in range(2, len(parts) + 1):
                    cand = ".".join(parts[:i])
                    for resolved in (cand, f"{cand}.__init__"):
                        if resolved in known:
                            deps.add(resolved)
            graph[module] = deps
    return graph


def _top_level_packages() -> list[str]:
    return sorted(p.name for p in (SRC / PKG).iterdir() if p.is_dir() and (p / "__init__.py").exists())


def _norm(module: str) -> str:
    return module.removesuffix(".__init__")


def test_no_path_from_triage_to_execution_engine_static() -> None:  # A2 hard gate
    # direct edges, including TYPE_CHECKING ones: not even a type-level coupling
    direct = [
        f"{m}:{line} imports {name}"
        for m, path in _modules("triage")
        for name, line in _imports(path, m)
        if _under(name, EXECUTION) or "ExecutionEngine" in name
    ]
    assert direct == []
    # transitive closure over runtime imports (what actually loads)
    graph = _runtime_import_graph()
    frontier = [m for m, _ in _modules("triage")]
    seen: set[str] = set()
    while frontier:
        mod = frontier.pop()
        if mod in seen:
            continue
        seen.add(mod)
        frontier.extend(graph.get(mod, ()))
    reached = sorted(m for m in seen if _under(_norm(m), EXECUTION))
    assert reached == [], f"triage transitively imports the execution engine via {reached}"
    # nor by name: no attribute/call on anything called execution/ExecutionEngine in the package source
    for _m, path in _modules("triage"):
        names = {n.id for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.Attribute)
        }
        assert not {n for n in names if n in {"ExecutionEngine", "run_proposal", "run_opening", "execution"}}, path


def test_no_path_from_triage_to_execution_engine_runtime(
    store: FileStore, monkeypatch: pytest.MonkeyPatch
) -> None:  # A2 hard gate
    from stockmanager.execution import engine

    def forbidden(*_a: Any, **_k: Any) -> None:
        raise AssertionError("triage reached the execution engine")

    for name in ("__init__", "run_proposal", "run_opening"):
        monkeypatch.setattr(engine.ExecutionEngine, name, forbidden)
    _write_fetch_log(store, [item(1), item(2, title="MSFT announces acquisition"), item(3, url="")])
    model = FixedTriageModel(HIGH)
    for cfg in (SHADOW, LIVE):  # the real wake path included
        run_triage(store, cfg, model, {"AAPL", "MSFT"})
        (store.root / "state" / "triage_done.json").unlink(missing_ok=True)
    # a fresh interpreter importing every triage module must not load the execution package at all
    modules = [m for m, _ in _modules("triage")]
    code = (
        "import importlib, sys\n"
        f"for m in {modules!r}: importlib.import_module(m)\n"
        "bad = sorted(k for k in sys.modules if k == 'stockmanager.execution' or k.startswith('stockmanager.execution.'))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=store.root.parent)
    assert out.stdout.strip() == ""


def test_escalation_goes_only_through_the_due_job_kind(store: FileStore) -> None:  # A2
    pipeline(store, FixedTriageModel(HIGH)).run([item()], NOON)
    wakes = tuple(WakeQueue(store.root).all())
    kinds = {j.kind for j in list_due_jobs(NOON, Config(), "LIVE", [], _with_wakes(*wakes))}
    assert kinds == {"triage_escalation"} and not {"execution", "opening", "reconfirm"} & kinds
    assert not (store.root / "proposals").exists() and not store.portfolio_exists()  # triage proposes and fills nothing


# -- off-cycle evaluation wake: engine accepts a triage cycle id, composition binds the handler (R7/D1) --------------


def _triage_slot(wake: TriageWake) -> ScheduleSlot:
    return ScheduleSlot(slot_time=wake.due_at, cycle_id=wake.cycle_id, late=False)


def test_engine_runs_a_triage_cycle_without_date_parsing(world: World) -> None:  # R7/D1
    from stockmanager.sim.reasoner import StubReasoner
    from tests.support.reasoner_output import pair_output

    world.seed()
    wake = _wake(datetime(2026, 10, 22, 9, 0, tzinfo=PT))  # a Thursday: not the weekly slot's date
    slot = _triage_slot(wake)
    with pytest.raises(ValueError):
        date.fromisoformat(slot.cycle_id)  # the old parse would have raised here
    reasoner = StubReasoner(pair_output())
    result = world.evaluation(reasoner).run(slot, Config())
    assert result.outcome == "completed"
    assert reasoner.inputs and reasoner.inputs[0].cycle_id == wake.cycle_id
    assert list((world.store.root / "alerts" / "pending").glob("*.json"))  # the cycle's report was queued


def test_engine_still_dates_a_normal_cycle_by_its_slot(world: World) -> None:  # R7/D1: no behaviour change
    from stockmanager.sim.reasoner import StubReasoner
    from tests.support.reasoner_output import pair_output
    from tests.support.world import EVAL_SLOT

    world.seed()
    assert EVAL_SLOT.cycle_id == EVAL_SLOT.slot_time.date().isoformat()
    assert world.evaluation(StubReasoner(pair_output())).run(EVAL_SLOT, Config()).outcome == "completed"


class _CountingEvaluation:
    """Stands in for ``EvaluationEngine``: records the slots it was asked to run."""

    def __init__(self, outcome: str = "completed") -> None:
        self.outcome = outcome
        self.slots: list[ScheduleSlot] = []

    def run(self, slot: ScheduleSlot, _cfg: Config) -> Any:
        from stockmanager.evaluation.engine import RunResult

        self.slots.append(slot)
        return RunResult(self.outcome)  # type: ignore[arg-type]


def _cfg_with(triage: TriageConfig) -> Config:
    return Config(triage=triage)


def test_composition_binds_the_handler_and_the_wake_dispatches_to_it(store: FileStore, clock: FakeClock) -> None:  # R7
    from stockmanager.app import make_triage_escalation

    _seed_live(store)
    wake = _wake(clock.now - timedelta(minutes=1))
    WakeQueue(store.root).enqueue(wake)
    evaluation = _CountingEvaluation()
    handler = make_triage_escalation(evaluation, lambda: _cfg_with(LIVE))  # type: ignore[arg-type]
    assert tick(clock, store, Config(), triage_escalation=handler) == 0
    assert [s.cycle_id for s in evaluation.slots] == [wake.cycle_id]
    assert scheduler_state_mod.load(store.root).triage_done == (wake.cycle_id,)


def test_create_app_wires_the_handler(tmp_path: Path) -> None:  # R7
    from stockmanager.app import create_app

    (tmp_path / "ws").mkdir()
    assert create_app(tmp_path / "ws").triage_escalation is not None


@pytest.mark.parametrize("cfg", [TriageConfig(), SHADOW, TriageConfig(enabled=True, shadow_mode=False)])
def test_handler_refuses_unless_escalation_is_live(store: FileStore, clock: FakeClock, cfg: TriageConfig) -> None:  # R1
    from stockmanager.app import make_triage_escalation

    _seed_live(store)
    wake = _wake(clock.now)
    WakeQueue(store.root).enqueue(wake)
    evaluation = _CountingEvaluation()
    handler = make_triage_escalation(evaluation, lambda: _cfg_with(cfg))  # type: ignore[arg-type]
    assert handler(_triage_slot(wake)) is False
    tick(clock, store, Config(), triage_escalation=handler)
    assert evaluation.slots == []  # disabled / shadow: the evaluation never ran, so no proposal and no fill
    assert scheduler_state_mod.load(store.root).triage_done == ()
    assert not (store.root / "proposals").exists() and not (store.root / "trades.log").exists()


def test_handler_leaves_the_wake_due_when_the_run_is_not_completed(store: FileStore, clock: FakeClock) -> None:  # R7
    from stockmanager.app import make_triage_escalation

    wake = _wake(clock.now)
    handler = make_triage_escalation(_CountingEvaluation("lock_busy"), lambda: _cfg_with(LIVE))  # type: ignore[arg-type]
    assert handler(_triage_slot(wake)) is False


def test_shadow_run_after_wake_path_emits_no_proposal_and_no_fill(store: FileStore, clock: FakeClock) -> None:  # R1
    from stockmanager.app import make_triage_escalation

    _seed_live(store)
    _write_fetch_log(store, [item()])
    assert run_triage(store, SHADOW, FixedTriageModel(HIGH), {"AAPL"}) == {"shadow_escalate": 1}
    evaluation = _CountingEvaluation()
    handler = make_triage_escalation(evaluation, lambda: _cfg_with(SHADOW))  # type: ignore[arg-type]
    assert tick(clock, store, Config(), triage_escalation=handler) == 0
    assert evaluation.slots == [] and WakeQueue(store.root).all() == []
    assert not (store.root / "proposals").exists() and not (store.root / "trades.log").exists()
    assert store.load_portfolio().cash == Decimal(10000)


def test_dedupe_is_checked_before_dispatch_on_backlog_rerun(store: FileStore, clock: FakeClock) -> None:  # R4
    from stockmanager.app import make_triage_escalation

    _seed_live(store)
    _write_fetch_log(store, [item(1)])
    held = {"AAPL"}
    model = FixedTriageModel(HIGH)
    evaluation = _CountingEvaluation()
    handler = make_triage_escalation(evaluation, lambda: _cfg_with(LIVE))  # type: ignore[arg-type]
    assert run_triage(store, LIVE, model, held) == {"escalate": 1}
    tick(clock, store, Config(), triage_escalation=handler)
    (store.root / "state" / "triage_done.json").unlink()  # lose the backlog cursor: the item is judged again
    assert run_triage(store, LIVE, model, held) == {"suppressed_duplicate": 1}  # dedupe stops it before any wake
    assert len(WakeQueue(store.root).all()) == 1
    tick(clock, store, Config(), triage_escalation=handler)
    assert len(evaluation.slots) == 1  # one wake, dispatched once
