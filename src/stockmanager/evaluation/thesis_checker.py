"""Thesis re-check for ``RECONFIRM`` replacements (AS §8.7; LP Q16 leaves the LLM version unspecified).

``ThesisChecker`` is the deterministic, mock-grade version: it reads a BUY leg's own thesis text and looks for a
recent fact about the same ticker that points the other way (a "strong outlook" thesis against a downgrade, an
"approval" thesis against a rejection). It also compares the fresh price with the price the proposal was sized at,
because a stock that has already run, or broken, away from the entry is not the trade the owner approved.

Evidence is only ever the extracted ``Fact`` rows and the validated valuation prices. Price figures that appear in
news text are never read (SS §6, TS INJ-06): ``Fact`` has no price field and ``description`` is matched for words
only. The check is advisory in one direction: a miss means "no contradiction found", never "confirmed".

``ThesisReview`` adapts it to the ``ReconfirmEngine`` seam, which hands over the document and the valuation and
leaves the choice of evidence to the checker.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from stockmanager.domain.docs import Fact, PriceTable, ProposalDoc, ProposalLeg
from stockmanager.evaluation.reconfirm import ThesisVerdict
from stockmanager.store import FileStore

#: A thesis cue (regex, matched on the lower-cased thesis) -> stems of fact wording that contradicts it.
CONTRADICTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"\b(outlook|guidance|forecast|raise[sd]?)\b", ("withdr", "cut", "lower", "slash", "downgrad", "warn")),
    (r"\b(strong|beat|beats|record|growth|upside)\b", ("miss", "shortfall", "weak", "slump", "plung", "downgrad")),
    (r"\b(upgrade[sd]?|rating)\b", ("downgrad", "cut", "lower", "suspend")),
    (r"\b(approval|approved|clearance|launch)\b", ("reject", "denied", "delay", "complete response", "withdr", "recall")),
    (r"\b(acquisition|merger|deal|buyback)\b", ("terminat", "collaps", "block", "abandon", "suspend")),
    (r"\b(settle|settlement|lawsuit|probe|investigation)\b", ("charged", "fined", "indict", "subpoena", "widen")),
)

DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_MAX_DRIFT_PCT = Decimal(10)
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class ThesisCheckResult:
    """Whether the thesis still holds, and why not when it does not."""

    thesis_holds: bool
    reason: str = ""


class ThesisChecker:
    """Keyword and price-drift thesis check. Pure: no clock, no I/O."""

    def __init__(self, *, max_drift_pct: Decimal = DEFAULT_MAX_DRIFT_PCT) -> None:
        self._max_drift_pct = max_drift_pct

    def check(self, proposal: ProposalDoc, news: Sequence[Fact], prices: Mapping[str, Decimal]) -> ThesisCheckResult:
        """Check every BUY leg of ``proposal``; the first contradiction found fails the whole proposal."""
        for leg in proposal.legs:
            if leg.action != "BUY":
                continue
            contradiction = self._contradicting_fact(leg, news)
            if contradiction is not None:
                return ThesisCheckResult(False, f"{leg.ticker}: {contradiction}")
            drift = self._drift(leg, prices)
            if drift is not None:
                return ThesisCheckResult(False, f"{leg.ticker}: {drift}")
        return ThesisCheckResult(True)

    @staticmethod
    def _contradicting_fact(leg: ProposalLeg, news: Sequence[Fact]) -> str | None:
        thesis = leg.thesis.lower()
        cues = [stems for pattern, stems in CONTRADICTIONS if re.search(pattern, thesis)]
        if not cues:
            return None
        for fact in news:
            if fact.ticker != leg.ticker:
                continue
            text = fact.description.lower()
            for stems in cues:
                hit = next((stem for stem in stems if re.search(rf"\b{re.escape(stem)}", text)), None)
                if hit is not None:
                    return f"a {fact.event} fact from {fact.date.isoformat()} ({fact.source_domain}) contradicts the thesis"
        return None

    def _drift(self, leg: ProposalLeg, prices: Mapping[str, Decimal]) -> str | None:
        now = prices.get(leg.ticker)
        if now is None or leg.est_price <= 0:
            return None
        move = (now - leg.est_price) / leg.est_price * HUNDRED
        if abs(move) <= self._max_drift_pct:
            return None
        return f"the price moved {move:+.1f}% since the proposal was sized ({leg.est_price} -> {now})"


class ThesisReview:
    """The ``ReconfirmEngine`` seam over ``ThesisChecker``: gathers recent stored facts and the valuation's prices."""

    def __init__(
        self, store: FileStore, checker: ThesisChecker | None = None, *, lookback_days: int = DEFAULT_LOOKBACK_DAYS
    ) -> None:
        self._store = store
        self._checker = checker or ThesisChecker()
        self._lookback = timedelta(days=lookback_days)

    def check(self, doc: ProposalDoc, valuation: PriceTable) -> ThesisVerdict:
        """``keep`` unless a fact from the last ``lookback_days`` sessions or the price move contradicts ``doc``."""
        since = valuation.session_date - self._lookback
        tickers = {leg.ticker for leg in doc.legs}
        facts = [f for f in self._store.load_facts() if f.ticker in tickers and f.date >= since]
        prices = {t: q.price for t, q in valuation.quotes.items()}
        result = self._checker.check(doc, facts, prices)
        return ThesisVerdict("keep") if result.thesis_holds else ThesisVerdict("void", result.reason)


__all__ = ["CONTRADICTIONS", "ThesisCheckResult", "ThesisChecker", "ThesisReview"]
