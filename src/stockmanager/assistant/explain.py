"""Explanation mode (v2 S1): "why did you propose P-...?" and "what is the thesis on <held ticker>?".

Read-only and deterministic. An answer is assembled only from stored records -- the Reasoner's persisted output
(``evaluations/eval-<cycle>.json``), the proposal files, the extracted facts and the book -- with no LLM call and no
judgment of its own. Every claim carries the ids of the stored records it came from, and ``verify_claims`` refuses
an answer containing a claim whose citations do not all resolve (``UnresolvedCitation``): nothing is paraphrased
into existence. Stored text is quoted as data, never interpreted.

The evaluation file format is owned by ``evaluation/eval_record.py`` (written by the evaluation engine's commit).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from stockmanager.commands.parser import PROPOSAL_ID_RE
from stockmanager.domain.docs import Fact, ProposalDoc
from stockmanager.evaluation.eval_record import StoredEvaluation, parse_evaluation
from stockmanager.evaluation.reasoner_io import HoldingAssessment, TradeIdea
from stockmanager.store import FileStore

POSITION_PREFIX = "position:"


class ExplainError(Exception):
    """Base of every refusal from explanation mode; the message is safe to show the user."""


class ProposalNotFound(ExplainError):
    """No stored proposal has the requested id."""


class ThesisNotFound(ExplainError):
    """The ticker is not a current holding, or nothing stored carries a thesis for it."""


class UnresolvedCitation(ExplainError):
    """A claim has no citation, or cites an id that is not a stored record."""


@dataclass(frozen=True)
class Claim:
    """One statement of an answer and the stored record ids it rests on."""

    text: str
    citations: tuple[str, ...]


@dataclass(frozen=True)
class Explanation:
    """A verified answer: what it is about and its claims, each with resolvable citations."""

    subject: str
    claims: tuple[Claim, ...]


@dataclass(frozen=True)
class StoredRecords:
    """A read-only snapshot of everything explanation mode may cite."""

    proposals: Mapping[str, ProposalDoc]
    evaluations: Mapping[str, StoredEvaluation]
    facts: Mapping[str, Fact]
    positions: Mapping[str, str]  # held ticker -> stored thesis

    def resolves(self, citation: str) -> bool:
        """True iff ``citation`` names a stored proposal, evaluation, fact or held position."""
        if citation.startswith(POSITION_PREFIX):
            return citation.removeprefix(POSITION_PREFIX) in self.positions
        return citation in self.proposals or citation in self.evaluations or citation in self.facts


def load_evaluations(root: Path) -> dict[str, StoredEvaluation]:
    """Every parseable ``evaluations/*.json`` under ``root``, by eval id. Unreadable or malformed files are skipped."""
    directory = root / "evaluations"
    out: dict[str, StoredEvaluation] = {}
    for path in sorted(directory.glob("*.json")) if directory.exists() else []:
        try:
            stored = parse_evaluation(path.read_text(encoding="utf-8"), path.stem)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        out[stored.eval_id] = stored
    return out


def load_records(store: FileStore) -> StoredRecords:
    """Read the stored records through the store's read methods only; nothing is written."""
    positions = {p.ticker: p.thesis for p in store.load_portfolio().positions} if store.portfolio_exists() else {}
    return StoredRecords(
        proposals={p.proposal_id: p for p in store.load_proposals()},
        evaluations=load_evaluations(store.root),
        facts={f.fact_id: f for f in store.load_facts()},
        positions=positions,
    )


def verify_claims(claims: Iterable[Claim], records: StoredRecords) -> None:
    """Raise ``UnresolvedCitation`` unless every claim has at least one citation and all of them are stored records."""
    for claim in claims:
        if not claim.citations:
            raise UnresolvedCitation(f"claim has no citation: {claim.text!r}")
        for citation in claim.citations:
            if not records.resolves(citation):
                raise UnresolvedCitation(f"claim cites {citation!r}, which is not a stored record: {claim.text!r}")


def _verified(subject: str, claims: list[Claim], records: StoredRecords) -> Explanation:
    verify_claims(claims, records)
    return Explanation(subject, tuple(claims))


def _fact_claims(fact_ids: Iterable[str], records: StoredRecords) -> list[Claim]:
    """One claim per cited fact, quoting its stored fields. An id with no stored fact fails here, not later."""
    out: list[Claim] = []
    for fact_id in dict.fromkeys(fact_ids):
        fact = records.facts.get(fact_id)
        if fact is None:
            raise UnresolvedCitation(f"the Reasoner cited fact {fact_id!r}, which is not a stored fact")
        out.append(
            Claim(f"{fact.ticker} {fact.event} ({fact.date.isoformat()}, {fact.source_domain}): {fact.description}", (fact_id,))
        )
    return out


def _linked_idea(proposal_id: str, records: StoredRecords) -> tuple[StoredEvaluation, TradeIdea] | None:
    for evaluation in sorted(records.evaluations.values(), key=lambda e: e.eval_id, reverse=True):
        priority = evaluation.proposal_ideas.get(proposal_id)
        for idea in evaluation.output.ideas:
            if priority is not None and idea.priority == priority:
                return evaluation, idea
    return None


def explain_proposal(proposal_id: str, records: StoredRecords) -> Explanation:
    """Why ``proposal_id`` was proposed, from its stored file, the idea it came from and that idea's cited facts."""
    proposal = records.proposals.get(proposal_id) if PROPOSAL_ID_RE.match(proposal_id) else None
    if proposal is None:
        raise ProposalNotFound(f"No stored proposal {proposal_id}")
    legs = "; ".join(f"{leg.action} {leg.ticker} ~{leg.est_shares} sh @ ~{leg.est_price}" for leg in proposal.legs)
    claims = [
        Claim(f"Proposed {legs} (state {proposal.state}).", (proposal_id,)),
        Claim(f"Rationale: {proposal.rationale}", (proposal_id,)),
        Claim(f"Alternative of holding: {proposal.hold_alternative}", (proposal_id,)),
    ]
    linked = _linked_idea(proposal_id, records)
    if linked is not None:
        evaluation, idea = linked
        eid = evaluation.eval_id
        claims += [
            Claim(f"Reasoner rationale: {idea.rationale}", (eid, *idea.evidence_fact_ids)),
            Claim(f"Why it beats holding: {idea.why_beats_hold}", (eid,)),
            Claim(f"Main risks: {'; '.join(idea.risk_assessment.main_risks)}", (eid,)),
            Claim(f"Invalidation: {idea.risk_assessment.invalidation}", (eid,)),
            *_fact_claims(idea.evidence_fact_ids, records),
        ]
    return _verified(proposal_id, claims, records)


def _latest_assessment(ticker: str, records: StoredRecords) -> tuple[StoredEvaluation, HoldingAssessment] | None:
    for evaluation in sorted(records.evaluations.values(), key=lambda e: e.eval_id, reverse=True):
        for holding in evaluation.output.holdings:
            if holding.ticker == ticker:
                return evaluation, holding
    return None


def explain_thesis(ticker: str, records: StoredRecords) -> Explanation:
    """The stored thesis for held ``ticker``: the book's thesis text plus the latest stored Reasoner assessment."""
    symbol = ticker.strip().upper()
    if symbol not in records.positions:
        raise ThesisNotFound(f"{symbol} is not a current holding")
    claims: list[Claim] = []
    if records.positions[symbol]:
        claims.append(Claim(f"Thesis at purchase: {records.positions[symbol]}", (f"{POSITION_PREFIX}{symbol}",)))
    assessed = _latest_assessment(symbol, records)
    if assessed is not None:
        evaluation, holding = assessed
        claims += [
            Claim(
                f"Latest Reasoner assessment: stance {holding.stance}, thesis {holding.thesis_status}.",
                (evaluation.eval_id, *holding.evidence_fact_ids),
            ),
            *_fact_claims(holding.evidence_fact_ids, records),
        ]
    if not claims:
        raise ThesisNotFound(f"Nothing stored carries a thesis for {symbol}")
    return _verified(symbol, claims, records)


def render(explanation: Explanation) -> str:
    """Plain text, one ``- claim [src: ids]`` line per claim (the digest's citation format)."""
    lines = [f"{explanation.subject}:"]
    lines += [f"- {' '.join(c.text.split())} [src: {', '.join(c.citations)}]" for c in explanation.claims]
    return "\n".join(lines)
