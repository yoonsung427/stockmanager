"""S1 explanation mode (docs/v2-test-plan.md §3): answers come only from stored reasoner output + stored facts, and
every citation resolves to a stored record. No LLM, no network: sim ``World`` fixtures on a temp workspace."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from stockmanager.assistant.explain import (
    Claim,
    ProposalNotFound,
    ThesisNotFound,
    UnresolvedCitation,
    explain_proposal,
    explain_thesis,
    load_records,
    render,
    verify_claims,
)
from stockmanager.cli import build_parser, main
from stockmanager.commands.confirm import INSTANT_VERBS, READ_VERBS
from stockmanager.config import Config
from stockmanager.domain.docs import Fact
from stockmanager.evaluation.reasoner import ReasonerFailure
from stockmanager.sim import StubReasoner
from tests.support.reasoner_output import buy_value_leg, idea, output
from tests.support.world import EVAL_SAT, EVAL_SLOT, PAIR_ID, World, pr_pair

EVAL_ID = "eval-2026-10-24"


def make_fact(fact_id: str, ticker: str = "MSFT") -> Fact:
    return Fact(
        fact_id=fact_id,
        ticker=ticker,
        event="earnings",
        description=f"{ticker} beat estimates.",
        date=date(2026, 10, 23),
        source_url="https://example.com/x",
        source_domain="example.com",
        display_title="t",
    )


def put_evaluation(world: World, fact_ids: list[str], *, link: bool = True) -> None:
    """Persist a Reasoner output the way an evaluation file holds it: output fields + ``eval_id`` + proposal link."""
    first = idea(1, buy_value_leg("buy1", "MSFT", "4000"))
    first["evidence_fact_ids"] = fact_ids
    out = output(first, held=("AAPL", "JPM", "MSFT"))
    holdings = out.model_dump(mode="json")
    holdings["holdings"][2]["evidence_fact_ids"] = fact_ids
    doc = {"eval_id": EVAL_ID, **holdings, "proposal_ideas": {PAIR_ID: 1} if link else {}}
    path = world.root / "evaluations" / f"{EVAL_ID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def seeded(world: World) -> World:
    world.seed()
    world.put(pr_pair(state="VETO_WINDOW"))
    world.store.save_extracted_facts("2026-10-24", {"facts": [make_fact("f1").model_dump(mode="json")]}, [])
    put_evaluation(world, ["f1"])
    return world


def test_proposal_answer_is_built_only_from_stored_records(seeded: World) -> None:
    records = load_records(seeded.store)
    answer = explain_proposal(PAIR_ID, records)
    text = {c.text for c in answer.claims}
    proposal = seeded.proposal(PAIR_ID)
    assert f"Rationale: {proposal.rationale}" in text
    assert "Reasoner rationale: rationale 1" in text
    assert "Why it beats holding: beats hold 1" in text
    assert "MSFT earnings (2026-10-23, example.com): MSFT beat estimates." in text
    # every claim's wording comes from a stored field: nothing else is in the answer
    assert all(c.citations for c in answer.claims)


def test_proposal_citations_resolve_to_stored_records(seeded: World) -> None:
    records = load_records(seeded.store)
    answer = explain_proposal(PAIR_ID, records)
    cited = {cid for c in answer.claims for cid in c.citations}
    assert cited == {PAIR_ID, EVAL_ID, "f1"}
    assert all(records.resolves(cid) for cid in cited)
    assert "[src: eval-2026-10-24, f1]" in render(answer)


def test_proposal_without_linked_evaluation_cites_the_proposal_file_only(seeded: World) -> None:
    put_evaluation(seeded, ["f1"], link=False)
    answer = explain_proposal(PAIR_ID, load_records(seeded.store))
    assert {cid for c in answer.claims for cid in c.citations} == {PAIR_ID}


def test_unknown_or_malformed_proposal_id_is_a_typed_error(seeded: World) -> None:
    records = load_records(seeded.store)
    with pytest.raises(ProposalNotFound):
        explain_proposal("P-2026-01-01-99", records)
    with pytest.raises(ProposalNotFound):
        explain_proposal("../portfolio", records)


def test_reasoner_citing_a_fact_that_is_not_stored_fails(seeded: World) -> None:
    put_evaluation(seeded, ["f1", "f-ghost"])
    with pytest.raises(UnresolvedCitation, match="f-ghost"):
        explain_proposal(PAIR_ID, load_records(seeded.store))
    with pytest.raises(UnresolvedCitation, match="f-ghost"):
        explain_thesis("MSFT", load_records(seeded.store))


def test_claim_with_no_matching_stored_fact_fails_verification(seeded: World) -> None:
    records = load_records(seeded.store)
    verify_claims([Claim("MSFT beat estimates.", ("f1",))], records)
    with pytest.raises(UnresolvedCitation):
        verify_claims([Claim("MSFT will double.", ("f-invented",))], records)
    with pytest.raises(UnresolvedCitation):
        verify_claims([Claim("Trust me.", ())], records)
    with pytest.raises(UnresolvedCitation):  # one good citation does not launder a bad one
        verify_claims([Claim("Mixed.", ("f1", "f-invented"))], records)


def test_thesis_answer_cites_book_assessment_and_facts(seeded: World) -> None:
    records = load_records(seeded.store)
    answer = explain_thesis("msft", records)
    texts = [c.text for c in answer.claims]
    assert texts[0].startswith("Thesis at purchase: ")
    assert "Latest Reasoner assessment: stance hold, thesis intact." in texts
    cited = {cid for c in answer.claims for cid in c.citations}
    assert cited == {"position:MSFT", EVAL_ID, "f1"}
    assert all(records.resolves(cid) for cid in cited)


def test_thesis_for_a_ticker_not_held_is_a_typed_error(seeded: World) -> None:
    with pytest.raises(ThesisNotFound):
        explain_thesis("NVDA", load_records(seeded.store))


def test_explain_is_read_only(seeded: World) -> None:
    before = {p: p.read_bytes() for p in seeded.root.rglob("*") if p.is_file()}
    records = load_records(seeded.store)
    explain_proposal(PAIR_ID, records)
    explain_thesis("MSFT", records)
    assert {p: p.read_bytes() for p in seeded.root.rglob("*") if p.is_file()} == before


def test_cli_explain_prints_cited_answers_and_refusals(seeded: World, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(seeded.root)
    assert main(["explain", PAIR_ID, "--workspace", root]) == 0
    out = capsys.readouterr().out
    assert PAIR_ID in out and "[src: " in out
    assert main(["explain", "--thesis", "AAPL", "--workspace", root]) == 0
    assert "position:AAPL" in capsys.readouterr().out
    assert main(["explain", "P-2026-01-01-99", "--workspace", root]) == 0
    assert capsys.readouterr().out.startswith("❌")
    assert main(["explain", "--workspace", root]) == 0
    assert capsys.readouterr().out.startswith("❌")


def test_explain_adds_no_irreversible_verb_to_the_allow_list() -> None:
    assert READ_VERBS == frozenset({"STATUS"})
    assert INSTANT_VERBS == frozenset({"PAUSE"})
    assert "explain" in build_parser().format_help()


def test_explain_module_makes_no_network_or_llm_import() -> None:
    source = (Path(__file__).parents[2] / "src/stockmanager/assistant/explain.py").read_text(encoding="utf-8")
    for banned in ("httpx", "anthropic", "openai", "stockmanager.llm", "stockmanager.adapters"):
        assert banned not in source


# -- end to end: the evaluation engine writes the file explain reads --------------------------------------------


def _run_cycle(world: World, fact_ids: list[str], *, with_idea: bool, failure: ReasonerFailure | None = None) -> None:
    world.seed()
    world.clock.set(EVAL_SAT)
    facts = [make_fact(f) for f in fact_ids]
    world.store.save_extracted_facts("2026-10-24", {"facts": [f.model_dump(mode="json") for f in facts]}, [])
    first = idea(1, buy_value_leg("buy1", "MSFT", "4000"))
    first["evidence_fact_ids"] = fact_ids
    out = output(*([first] if with_idea else []), held=("AAPL", "JPM", "MSFT"))
    out.holdings[2].evidence_fact_ids = fact_ids
    result = world.evaluation(StubReasoner(out, failure), facts=facts).run(EVAL_SLOT, Config())
    assert result.outcome == "completed"


def test_engine_written_evaluation_is_read_by_explain_with_resolving_citations(world: World) -> None:
    _run_cycle(world, ["f1", "f2"], with_idea=True)
    path = world.root / "evaluations" / "eval-2026-10-24.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["eval_id"] == EVAL_ID and doc["cycle_date"] == "2026-10-24" and "prompt_sha256" in doc
    (pid,) = doc["proposal_ideas"]
    assert doc["proposal_ideas"][pid] == 1 and doc["ideas"][0]["evidence_fact_ids"] == ["f1", "f2"]

    records = load_records(world.store)
    assert records.evaluations[EVAL_ID].proposal_ideas == {pid: 1}
    answer = explain_proposal(pid, records)
    cited = {c for claim in answer.claims for c in claim.citations}
    assert {"f1", "f2", EVAL_ID, pid} <= cited
    assert all(records.resolves(c) for c in cited)
    assert all(fid in records.facts for fid in doc["ideas"][0]["evidence_fact_ids"])
    thesis = explain_thesis("MSFT", records)
    assert {"f1", "f2"} <= {c for claim in thesis.claims for c in claim.citations}


def test_quiet_cycle_still_persists_the_reasoner_output(world: World) -> None:
    _run_cycle(world, ["f1"], with_idea=False)
    doc = json.loads((world.root / "evaluations" / "eval-2026-10-24.json").read_text(encoding="utf-8"))
    assert doc["ideas"] == [] and doc["proposal_ideas"] == {} and doc["holdings"][2]["evidence_fact_ids"] == ["f1"]
    assert "f1" in {c for claim in explain_thesis("MSFT", load_records(world.store)).claims for c in claim.citations}


def test_failed_reasoner_writes_no_evaluation_file(world: World) -> None:
    _run_cycle(world, [], with_idea=True, failure=ReasonerFailure("reasoner_schema"))
    assert not (world.root / "evaluations" / "eval-2026-10-24.json").exists()
