"""``ThesisChecker`` / ``ThesisReview`` (AS §8.7): keyword contradiction, price drift, and the reconfirm wiring."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from stockmanager.domain.docs import Fact, PriceQuote, PriceTable, ProposalDoc
from stockmanager.evaluation.thesis_checker import ThesisChecker, ThesisCheckResult, ThesisReview
from stockmanager.store import FileStore
from tests.support.books import PT
from tests.support.proposals import make_proposal

DAY = date(2026, 10, 23)


def proposal(thesis: str, ticker: str = "AAPL", est_price: str = "200") -> ProposalDoc:
    doc = make_proposal()
    leg = doc.legs[0].model_copy(update={"thesis": thesis, "ticker": ticker, "est_price": Decimal(est_price)})
    return doc.model_copy(update={"legs": [leg]})


def fact(description: str, ticker: str = "AAPL", day: date = DAY, fact_id: str = "f1") -> Fact:
    return Fact(
        fact_id=fact_id,
        ticker=ticker,
        event="rating_change",
        description=description,
        date=day,
        source_url="https://example.com/a",
        source_domain="example.com",
        display_title="t",
    )


PRICES = {"AAPL": Decimal(200)}


def test_no_news_and_no_drift_means_the_thesis_holds() -> None:
    assert ThesisChecker().check(proposal("strong outlook"), [], PRICES) == ThesisCheckResult(True)


def test_a_downgrade_contradicts_a_strong_outlook_thesis() -> None:
    result = ThesisChecker().check(proposal("Strong outlook after the product cycle"), [fact("Analyst downgrades AAPL to sell")], PRICES)
    assert not result.thesis_holds and result.reason.startswith("AAPL:") and "example.com" in result.reason


def test_an_approval_thesis_is_contradicted_by_a_rejection() -> None:
    result = ThesisChecker().check(proposal("Pending FDA approval"), [fact("The agency rejected the application")], PRICES)
    assert not result.thesis_holds


def test_facts_about_other_tickers_and_unrelated_wording_do_not_count() -> None:
    news = [fact("Analyst downgrades MSFT", ticker="MSFT"), fact("AAPL opens a new store")]
    assert ThesisChecker().check(proposal("strong outlook"), news, PRICES).thesis_holds


def test_a_thesis_with_no_recognised_cue_cannot_be_contradicted_by_keywords() -> None:
    assert ThesisChecker().check(proposal("looks cheap"), [fact("Analyst downgrades AAPL")], PRICES).thesis_holds


def test_stems_match_word_starts_only() -> None:
    assert ThesisChecker().check(proposal("strong outlook"), [fact("Scut-work contract renewed")], PRICES).thesis_holds


def test_price_drift_beyond_the_limit_fails_the_thesis() -> None:
    result = ThesisChecker().check(proposal("looks cheap"), [], {"AAPL": Decimal(230)})
    assert not result.thesis_holds and "+15.0%" in result.reason
    assert ThesisChecker().check(proposal("looks cheap"), [], {"AAPL": Decimal(220)}).thesis_holds  # exactly +10%
    assert not ThesisChecker(max_drift_pct=Decimal(5)).check(proposal("x"), [], {"AAPL": Decimal(189)}).thesis_holds


def test_sell_legs_are_not_checked() -> None:
    doc = proposal("strong outlook")
    sell = doc.legs[0].model_copy(update={"action": "SELL"})
    assert ThesisChecker().check(doc.model_copy(update={"legs": [sell]}), [fact("downgrade")], {"AAPL": Decimal(999)}).thesis_holds


def _table(price: str) -> PriceTable:
    now = make_proposal().created_at.astimezone(PT)
    quote = PriceQuote("AAPL", DAY, "close", Decimal(price), "yahoo", now)
    return PriceTable(source="yahoo", session_date=DAY, quotes={"AAPL": quote})


def _store_with_facts(tmp_path: Path, rows: list[Fact]) -> FileStore:
    store = FileStore(tmp_path)
    store.save_extracted_facts("2026-10-24", {"facts": [r.model_dump(mode="json") for r in rows]}, [])
    return store


def test_review_voids_on_a_recent_contradicting_fact(tmp_path: Path) -> None:
    review = ThesisReview(_store_with_facts(tmp_path, [fact("downgrade to sell")]))
    verdict = review.check(proposal("strong outlook"), _table("200"))
    assert verdict.action == "void" and verdict.reason.startswith("AAPL:")


def test_review_ignores_facts_older_than_the_lookback(tmp_path: Path) -> None:
    old = fact("downgrade to sell", day=date(2026, 10, 1))
    assert ThesisReview(_store_with_facts(tmp_path, [old])).check(proposal("strong outlook"), _table("200")).action == "keep"


def test_review_keeps_when_there_are_no_stored_facts(tmp_path: Path) -> None:
    assert ThesisReview(FileStore(tmp_path)).check(proposal("strong outlook"), _table("200")).action == "keep"
