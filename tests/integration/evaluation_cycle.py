"""Full evaluation cycle: news fetch -> extract -> Reasoner -> ProposalBuilder -> outbox -> delivery (TS E2E-01, E2E-02).

Only the network edges are doubles: fake news fetcher, fake extractor, stub Reasoner, scripted prices, recording channels.
The news stage, price service, engine, builder, store transaction, outbox and delivery loop are the real code.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

from stockmanager.config import Config
from stockmanager.domain.docs import PortfolioDoc
from stockmanager.evaluation.engine import RunResult
from stockmanager.evaluation.reasoner import ReasonerFailure
from stockmanager.evaluation.reasoner_io import ReasonerInput, ReasonerOutput
from stockmanager.news import NewsResult
from stockmanager.news.config import ExtractorConfig, NewsConfig, SourceConfig
from stockmanager.news.pipeline import run_news_stage
from stockmanager.news.plan import NewsQuery
from stockmanager.notify.deliver import deliver_pending
from stockmanager.scheduler import state as scheduler_state
from stockmanager.scheduler.tick import tick
from stockmanager.sim import RecordingChannel, StubReasoner
from tests.support.assertions import alerts, assert_lockfree, assert_nofill, assert_recon, books, workspace_hashes
from tests.support.news import FakeExtractor, FakeFetcher, candidate, raw
from tests.support.reasoner_output import hold_output, pair_output
from tests.support.world import EVAL_SAT, EVAL_SLOT, PAIR_ID, World

FRIDAY = date(2026, 10, 23)
ALL = ["holding", "analyst", "earnings", "benchmark", "sector", "macro"]
HEADLINE_MARKER = "UNIQUE-HEADLINE-TEXT"


def news_cfg() -> NewsConfig:
    """One sector ETF, serial extraction; only searxng has a fetcher behind it in these tests."""
    return NewsConfig(
        sector_etfs=["XLK"],
        extractor=ExtractorConfig(concurrency=1),
        sources={
            "searxng": SourceConfig(max_concurrency=1, kinds=ALL),
            "finnhub": SourceConfig(max_concurrency=1, kinds=["holding", "analyst", "earnings", "macro"]),
            "yahoo_rss": SourceConfig(max_concurrency=1, kinds=["holding"]),
        },
    )


def headline(q: NewsQuery) -> list[Any]:
    """One article per query; the title carries a marker the Reasoner must never see."""
    return [
        raw(
            title=f"{HEADLINE_MARKER} {q.ticker} {q.kind}",
            url=f"https://news.example.com/{q.kind}/{q.ticker}",
            published=FRIDAY,
        )
    ]


def about_focus(item: Any) -> list[Any]:
    """The extractor reads each item into one typed fact about the ticker the query was for."""
    return [
        candidate(
            ticker=item.focus_ticker or "SPY",
            event="other",
            description=f"News about {item.focus_ticker} {item.item_id[:6]}.",
            when=FRIDAY,
        )
    ]


class Rig:
    """A world plus the real news stage over fake fetcher/extractor, and the two recording channels."""

    def __init__(self, world: World, output: ReasonerOutput) -> None:
        self.world = world
        self.fetcher = FakeFetcher("searxng", headline)
        self.extractor = FakeExtractor(about_focus)
        self.reasoner = StubReasoner(output)
        self.discord = RecordingChannel("discord", world.clock)
        self.mainchat = RecordingChannel("mainchat", world.clock)
        self.engine = world.evaluation(self.reasoner, news_runner=self._news)

    def _news(self, cycle_id: str, portfolio: PortfolioDoc) -> NewsResult:
        return run_news_stage(
            cycle_id, portfolio, news_cfg(), self.world.clock, [self.fetcher], self.extractor, store=self.world.store
        )

    def run(self) -> RunResult:
        """One evaluation for the Saturday slot."""
        return self.engine.run(EVAL_SLOT, Config())

    def seen(self) -> ReasonerInput:
        """The one ``ReasonerInput`` the Reasoner was given."""
        (inp,) = self.reasoner.inputs
        return inp


def test_e2e01_hold_week_writes_only_the_report_alert(world: World) -> None:
    """TS E2E-01: a cycle whose Reasoner proposes nothing creates no proposal and moves no money."""
    world.seed()
    world.clock.set(EVAL_SAT)
    rig = Rig(world, hold_output())
    before = books(world.store)

    assert rig.run().outcome == "completed"

    assert_nofill(before, world.store)
    assert not (world.root / "proposals").exists()
    assert [a["kind"] for a in alerts(world.root)] == ["evaluation_complete"]
    assert_lockfree(world.store)


def test_e2e02_stages_1_to_5_news_to_delivered_alerts(world: World) -> None:
    """TS E2E-02 (SS §8.4 stages 1-5): facts are fetched and extracted, only typed facts reach the Reasoner, its pair
    intent becomes one validated PROPOSED proposal, the alerts queue in the same commit, and delivery sends them."""
    world.seed()
    world.clock.set(EVAL_SAT)
    rig = Rig(world, pair_output())
    before = books(world.store)

    assert rig.run().outcome == "completed"

    # news fetch -> extract: every query was answered, every item became a fact, and they were persisted
    assert rig.fetcher.calls and len(rig.extractor.items) == len(rig.fetcher.calls)
    persisted = json.loads((world.root / "extracted-facts" / "2026-10-24.json").read_text(encoding="utf-8"))
    assert persisted["news_status"] == "ok" and len(persisted["facts"]) == len(rig.extractor.items)

    # -> Reasoner: the typed projection only; no headline text, no URL, no title, no price from the news
    seen = rig.seen()
    assert len(seen.facts) == len(persisted["facts"])
    assert set(seen.facts[0].model_dump()) == {"fact_id", "ticker", "event", "description", "date", "source_domain"}
    dumped = seen.model_dump_json()
    assert HEADLINE_MARKER not in dumped and "https://" not in dumped
    assert seen.price_source == "yahoo" and seen.valuation_date == "2026-10-23"

    # -> ProposalBuilder: one PROPOSED pair, numbers computed by code (25 AAPL = 25% of 100; 12.5 MSFT = 5,000 / 400)
    (path,) = (world.root / "proposals").glob("*.json")
    proposal = world.proposal(path.stem)
    assert proposal.proposal_id == PAIR_ID and proposal.state == "PROPOSED" and proposal.approval_mode == "auto"
    sell, buy = proposal.legs
    assert (sell.ticker, sell.est_shares, buy.ticker, buy.est_shares) == ("AAPL", Decimal(25), "MSFT", Decimal("12.5"))
    assert proposal.execute_at.isoformat() == "2026-10-26T06:30:00-07:00"
    assert_nofill(before, world.store)  # evaluating never fills

    # -> outbox, in the commit: the report alert and the veto-window alert, the latter with its literal command
    queued = alerts(world.root)
    assert sorted(a["kind"] for a in queued) == ["evaluation_complete", "veto_window_open"]
    veto = next(a for a in queued if a["kind"] == "veto_window_open")
    assert veto["needs_action"] and veto["commands"][0] == f"VETO {PAIR_ID} [reason]"  # AC-04: the literal command

    # -> delivery: both channels get every alert once, the veto alert first (priority lane, v2 P0); the veto deadline is bound at delivery time
    world.clock.advance(minutes=5)
    deliver_pending(world.store, [rig.discord, rig.mainchat], world.clock)
    ids = [veto["alert_id"], *(a["alert_id"] for a in queued if a is not veto)]
    assert [n for n, _ in rig.discord.sent] == ids == [n for n, _ in rig.mainchat.sent]
    assert list((world.root / "alerts" / "pending").glob("*.json")) == []
    assert len(list((world.root / "alerts" / "sent").glob("*.json"))) == len(ids)
    veto_embed = next(r.embed for _n, r in rig.discord.sent if r.embed and PAIR_ID in json.dumps(r.embed))
    assert veto_embed is not None and "2026-10-24T13:05:00" in veto_embed["description"]  # delivery 09:05 + 4 h
    assert_recon(world.store)


def test_a_tick_drives_the_cycle_and_the_next_tick_delivers_and_opens_the_window(world: World) -> None:
    """TS E2E-02 via the dispatcher: tick 1 evaluates, tick 2 drains the outbox and opens the veto window, tick 3 is a no-op."""
    world.seed()
    world.clock.set(EVAL_SAT)
    rig = Rig(world, pair_output())
    channels = [rig.discord, rig.mainchat]

    assert tick(world.clock, world.store, Config(), evaluation=rig.engine, channels=channels) == 0
    assert len(rig.reasoner.inputs) == 1
    assert scheduler_state.load(world.root).completed_slots == (date(2026, 10, 24),)
    assert rig.discord.sent == []  # the drain precedes the job in a tick, so alerts wait for the next one

    world.clock.advance(minutes=1)
    assert tick(world.clock, world.store, Config(), evaluation=rig.engine, channels=channels) == 0
    assert len(rig.discord.sent) == len(rig.mainchat.sent) == 2
    assert world.proposal(PAIR_ID).state == "VETO_WINDOW"

    world.clock.advance(minutes=1)
    tick(world.clock, world.store, Config(), evaluation=rig.engine, channels=channels)
    assert len(rig.reasoner.inputs) == 1 and len(rig.discord.sent) == 2  # not re-evaluated, not re-sent


def test_a_reasoner_failure_is_a_quiet_cycle_not_a_crash(world: World) -> None:
    """TS §8 / AS §5.2: no usable Reasoner output means no proposals; the cycle still completes and reports."""
    world.seed()
    world.clock.set(EVAL_SAT)
    rig = Rig(world, hold_output())
    rig.reasoner.failure = ReasonerFailure("reasoner_timeout", infra=True)

    result = rig.run()

    assert (result.outcome, result.detail) == ("completed", "reasoner_timeout")
    assert not (world.root / "proposals").exists()
    assert [a["kind"] for a in alerts(world.root)] == ["evaluation_complete"]


def test_a_book_that_changes_during_the_cycle_aborts_the_commit(world: World) -> None:
    """AS §1.2 (AD1): phase B runs lock-free; if ``rev`` moved while the Reasoner thought, nothing is committed."""
    world.seed()
    world.clock.set(EVAL_SAT)
    rig = Rig(world, pair_output())

    class Racing(StubReasoner):
        def evaluate(self, input: ReasonerInput) -> ReasonerOutput:
            with world.store.transaction("cmd:racing") as txn:  # a command lands mid-cycle and bumps rev
                txn.stage_portfolio(txn.state)
            return super().evaluate(input)

    racing = Racing(pair_output())
    engine = world.evaluation(racing, news_runner=rig._news)
    before = workspace_hashes(world.root)

    result = engine.run(EVAL_SLOT, Config())

    assert result.outcome == "aborted_state_changed"
    assert not (world.root / "proposals").exists()
    assert alerts(world.root) == []
    written = set(workspace_hashes(world.root)) - set(before)
    # phase B output only (news facts plus the D8 fetch-log capture)
    assert written <= {
        "extracted-facts/2026-10-24.json",
        "extracted-facts/2026-10-24.rejected.jsonl",
        "fetch-log/2026-10-24.jsonl",
    }
