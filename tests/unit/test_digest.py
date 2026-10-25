"""Digest narrative (v2 R5/R6/R9/R10): sourced claims, capped size, personalisation, quiet hours, no direct delivery."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from stockmanager.config import Config
from stockmanager.config.triage import TriageConfig
from stockmanager.domain.docs import Fact
from stockmanager.evaluation.reasoner_io import FiveDims, HoldingAssessment, ReasonerOutput
from stockmanager.jobs.common import dump_last_close
from stockmanager.jobs.digest import (
    NARRATIVE_CLAIM_MAX_CHARS,
    NARRATIVE_MAX_CHARS,
    NarrativeClaim,
    build_narrative,
    digest_release_at,
    move_claims,
    personalise,
    reasoner_claims,
    run_digest,
    veto_counts,
)
from stockmanager.llm.summary import NewsItem
from stockmanager.notify.outbox import Alert
from stockmanager.sim.prices import ScriptedPriceProvider
from stockmanager.store import FileStore
from stockmanager.triage.quiet import QuietHours
from tests.support.books import FakePidChecker, FixedClock, make_portfolio, make_trade
from tests.support.proposals import advance, make_proposal

PT = ZoneInfo("America/Los_Angeles")
FRI = date(2026, 10, 23)


def at(h: int, m: int = 0) -> datetime:
    return datetime(2026, 10, 23, h, m, tzinfo=PT)


def fact(fact_id: str, ticker: str = "MSFT", day: date = FRI, description: str = "Beat estimates.") -> Fact:
    return Fact(
        fact_id=fact_id,
        ticker=ticker,
        event="earnings",
        description=description,
        date=day,
        source_url="https://example.com/x",
        source_domain="example.com",
        display_title="t",
    )


def assessment(ticker: str, evidence: list[str]) -> HoldingAssessment:
    dims = FiveDims(thesis="t", momentum="m", catalysts="c", risk="r", opportunity_cost="o")
    return HoldingAssessment(
        ticker=ticker, stance="hold", thesis_status="intact", dims=dims, evidence_fact_ids=evidence
    )


def output(*holdings: HoldingAssessment) -> ReasonerOutput:
    return ReasonerOutput(summary="", hold_alternative="", holdings=list(holdings), ideas=[])


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(at(13, 30))


@pytest.fixture
def store(tmp_path: Path, clock: FixedClock) -> FileStore:
    s = FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)
    for sub in ("state", "alerts/pending", "extracted-facts"):
        (s.root / sub).mkdir(parents=True)
    trades = [make_trade(1, ticker="MSFT", shares="10", price="400"), make_trade(2, ticker="AAPL")]
    s.save_portfolio(make_portfolio(trades))
    reset_last_close(s)
    return s


def reset_last_close(s: FileStore) -> None:
    (s.root / "state" / "last_close.json").write_text(dump_last_close({"MSFT": Decimal(400), "AAPL": Decimal(100)}))


def write_facts(store: FileStore, *facts: Fact) -> None:
    rows = [json.loads(f.model_dump_json()) for f in facts]
    (store.root / "extracted-facts" / "c1.json").write_text(json.dumps({"facts": rows}))


def run(store: FileStore, clock: FixedClock, config: Config | None = None, **kw: Any) -> list[str]:
    """MSFT +2% (notable), AAPL +0.5% with news (notable)."""
    prices = ScriptedPriceProvider("yahoo", {"MSFT": Decimal(408), "AAPL": Decimal("100.5")}, clock)
    return run_digest(
        store,
        config or Config(),
        lambda *_: "It moved.",
        (),
        clock,
        price_provider=prices,
        news_lookup=lambda t, d: [NewsItem("h")] if t == "AAPL" else [],
        **kw,
    )


def pending(store: FileStore) -> list[Alert]:
    return [Alert.from_json(p.read_text()) for p in sorted((store.root / "alerts" / "pending").glob("*.json"))]


# -- grounding -----------------------------------------------------------------------------------------------------


def test_every_rendered_claim_cites_a_stored_record() -> None:
    claims = [NarrativeClaim("MSFT beat.", ("f1",), "MSFT"), NarrativeClaim("AAPL ok.", ("f2", "f1"))]
    n = build_narrative(claims, {"f1", "f2"})
    assert n.claims == 2 and n.rejected == 0
    assert "- MSFT beat. [src: f1]" in n.text and "- AAPL ok. [src: f2, f1]" in n.text


@pytest.mark.parametrize(
    "claim",
    [
        NarrativeClaim("No source.", ()),
        NarrativeClaim("Unknown source.", ("nope",)),
        NarrativeClaim("Partly known.", ("f1", "nope")),
        NarrativeClaim("   ", ("f1",)),
    ],
)
def test_unsourced_claims_are_rejected_not_softened(claim: NarrativeClaim) -> None:
    n = build_narrative([claim, NarrativeClaim("Good.", ("f1",))], {"f1"})
    assert n.rejected == 1 and n.claims == 1
    assert claim.text.strip() not in n.text or not claim.text.strip()
    assert "Good." in n.text


def test_all_claims_rejected_yields_no_section() -> None:
    assert build_narrative([NarrativeClaim("x", ())], {"f1"}).text == ""


def test_move_attribution_uses_only_recent_facts_for_the_moved_ticker() -> None:
    facts = [
        fact("f-new"),
        fact("f-old", day=FRI - timedelta(days=10)),
        fact("f-other", ticker="AAPL"),
        fact("f-future", day=FRI + timedelta(days=1)),
    ]
    claims = move_claims({"MSFT": Decimal("2.0000")}, facts, FRI)
    assert [c.source_ids for c in claims] == [("f-new",)]
    assert "MSFT moved +2.00%" in claims[0].text and "caused" not in claims[0].text


def test_move_without_a_fact_gets_no_claim() -> None:
    assert move_claims({"MSFT": Decimal(2)}, [], FRI) == []


def test_reasoner_claims_cite_the_evaluation_and_its_evidence_and_skip_unevidenced() -> None:
    out = output(assessment("MSFT", ["f1"]), assessment("AAPL", []))
    (claim,) = reasoner_claims("E-1", out)
    assert claim.source_ids == ("E-1", "f1") and claim.text == "MSFT: Reasoner stance hold, thesis intact"


def test_reasoner_evidence_not_in_stored_facts_is_rejected() -> None:
    (claim,) = reasoner_claims("E-1", output(assessment("MSFT", ["ghost"])))
    assert build_narrative([claim], {"E-1", "f1"}).rejected == 1


# -- size cap ------------------------------------------------------------------------------------------------------


def test_narrative_is_capped_and_drops_whole_claims_in_priority_order() -> None:
    claims = [NarrativeClaim(f"claim {i} " + "x" * 200, ("f1",)) for i in range(50)]
    n = build_narrative(claims, {"f1"})
    assert len(n.text) <= NARRATIVE_MAX_CHARS
    assert n.claims + n.dropped == 50 and n.dropped > 0
    assert f"claim {n.claims - 1} " in n.text and f"claim {n.claims} " not in n.text


def test_one_long_claim_is_truncated_to_the_claim_cap() -> None:
    n = build_narrative([NarrativeClaim("y" * 5000, ("f1",))], {"f1"})
    body = n.text.splitlines()[1]
    assert body.count("y") == NARRATIVE_CLAIM_MAX_CHARS - 1 and "…" in body


def test_digest_alert_keeps_structured_fields_free_of_narrative_text(store: FileStore, clock: FixedClock) -> None:
    write_facts(store, *[fact(f"f{i}", description="z" * 5000) for i in range(30)])
    run(store, clock)
    (alert,) = pending(store)
    assert all(len(v) < 20 for v in alert.fields.values())
    assert alert.body.count("z") <= NARRATIVE_MAX_CHARS


# -- end to end ----------------------------------------------------------------------------------------------------


def test_digest_appends_sourced_narrative_and_leaves_summaries_unchanged(store: FileStore, clock: FixedClock) -> None:
    write_facts(store, fact("f1"))
    out = run(store, clock, reasoner_outputs={"E-1": output(assessment("MSFT", ["f1"]))})
    assert sorted(out) == ["**AAPL** — It moved.", "**MSFT** — It moved."]
    (alert,) = pending(store)
    assert "**MSFT** — It moved." in alert.body
    assert "MSFT moved +2.00%" in alert.body and "[src: f1]" in alert.body and "[src: E-1, f1]" in alert.body
    assert alert.fields["holdings"] == "2" and alert.fields["narrative_claims"] == "2"


def test_digest_without_facts_matches_the_structured_only_output(store: FileStore, clock: FixedClock) -> None:
    run(store, clock)
    (alert,) = pending(store)
    assert "Narrative" not in alert.body and "narrative_claims" not in alert.fields


# -- personalisation -----------------------------------------------------------------------------------------------


def vetoed(pid: str, ticker: str) -> Any:
    doc = advance(make_proposal(pid, state="PROPOSED"), "VETOED", at(9), actor="owner", cause="vetoed")
    leg = doc.legs[0].model_copy(update={"ticker": ticker})
    return doc.model_copy(update={"legs": [leg]})


def test_veto_counts_come_from_vetoed_proposals_only() -> None:
    docs = [vetoed("P-1", "AAPL"), vetoed("P-2", "AAPL"), vetoed("P-3", "MSFT"), make_proposal("P-4", state="PROPOSED")]
    assert veto_counts(docs) == {"AAPL": 2, "MSFT": 1}


def test_most_vetoed_tickers_lead_and_ties_keep_input_order() -> None:
    claims = [
        NarrativeClaim("a", ("x",), "MSFT"),
        NarrativeClaim("b", ("x",), "AAPL"),
        NarrativeClaim("c", ("x",), "TSLA"),
    ]
    assert [c.text for c in personalise(claims, {"AAPL": 3})] == ["b", "a", "c"]
    assert personalise(claims, {}) == claims


def test_s4_join_is_inert() -> None:
    claims = [NarrativeClaim("a", ("x",), "MSFT"), NarrativeClaim("b", ("x",), "AAPL")]
    vetoes = {"AAPL": 1}
    assert personalise(claims, vetoes, {"AAPL": "anything", "MSFT": 99}) == personalise(claims, vetoes)


def test_s4_join_does_not_change_the_digest(store: FileStore, clock: FixedClock) -> None:
    write_facts(store, fact("f1"))
    run(store, clock)
    reset_last_close(store)
    run(store, clock, s4_join={"MSFT": "x"})
    first, second = pending(store)
    assert first.body == second.body and first.fields == second.fields


# -- quiet hours and delivery --------------------------------------------------------------------------------------


def test_release_boundary_is_the_end_of_quiet_hours() -> None:
    quiet = QuietHours.parse("22:00-06:30")
    assert digest_release_at(at(6, 29), quiet) == at(6, 30)
    assert digest_release_at(at(6, 30), quiet) == at(6, 30)  # quiet hours are [start, end)
    assert digest_release_at(at(13, 0), quiet) == at(13, 0)
    assert digest_release_at(at(22, 0), quiet) == at(6, 30) + timedelta(days=1)


def test_digest_made_inside_quiet_hours_is_held_until_they_end(store: FileStore, clock: FixedClock) -> None:
    run(store, clock, Config(triage=TriageConfig(quiet_hours="13:00-14:00")))  # the fixture clock is 13:30
    (alert,) = pending(store)
    assert {c.next_attempt_at for c in alert.channels.values()} == {at(14, 0)}


def test_digest_made_outside_quiet_hours_is_immediately_deliverable(store: FileStore, clock: FixedClock) -> None:
    run(store, clock)
    (alert,) = pending(store)
    assert {c.next_attempt_at for c in alert.channels.values()} == {None}


def test_digest_delivers_only_through_the_single_writer_pass(store: FileStore, clock: FixedClock) -> None:
    class Boom:
        name = "discord"

        def send(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("the digest must not send; the outbox is the single writer")

    prices = ScriptedPriceProvider("yahoo", {"MSFT": Decimal(408), "AAPL": Decimal("100.5")}, clock)
    channels: Any = [Boom()]
    run_digest(store, Config(), lambda *_: "s", channels, clock, price_provider=prices, news_lookup=lambda t, d: [])
    assert len(pending(store)) == 1
    # Single writer: the digest never calls channel.send itself; delivery goes only through
    # notify.deliver_pending (whose failures leave the alert in the outbox, and which respects the
    # quiet-hours next_attempt_at hold asserted above). Boom raises inside that path, so the
    # delivery pass absorbs it and the alert stays pending.
    assert pending(store)[0].channels["discord"].next_attempt_at is None
