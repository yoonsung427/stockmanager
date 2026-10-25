"""Raw-text pre-filter (v2 design D1, R7): held ticker + event-kind keyword hints. Pure: no I/O, no LLM call.

This is the hard gate. It runs on the raw headline and snippet locally so that the classifier only ever sees items
that name a held ticker AND look like an event of a known kind. Keyword hints are deliberately coarse; the
classifier decides, this only decides whether it is worth asking.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Final

from stockmanager.triage.models import EventKind, TriageItem

_HINTS: Final[dict[EventKind, str]] = {
    EventKind.EARNINGS: r"earnings|quarterly results|\bEPS\b|beats? estimates|misses? estimates|revenue (?:rose|fell|jumped|dropped)",
    EventKind.GUIDANCE: r"guidance|outlook|forecast|raises? (?:its )?(?:full-year|annual)|cuts? (?:its )?(?:full-year|annual)",
    EventKind.MERGER_ACQUISITION: r"acqui(?:re|res|red|sition)|merger|merges|takeover|buyout|to be bought|tender offer",
    EventKind.REGULATORY: r"\bSEC\b|\bFDA\b|\bFTC\b|\bDOJ\b|antitrust|regulator|approval|approves|investigation|subpoena",
    EventKind.LEGAL: r"lawsuit|sues|sued|settlement|class action|verdict|indict",
    EventKind.MANAGEMENT_CHANGE: r"\bCEO\b|\bCFO\b|steps down|resigns?|appoints?|names new|chairman",
    EventKind.ANALYST_RATING: r"downgrad|upgrad|price target|initiates coverage|analyst",
    EventKind.CAPITAL_RETURN: r"dividend|buyback|share repurchase|stock split|special distribution",
    EventKind.DISTRESS: r"bankruptcy|chapter 11|going concern|default(?:s|ed)?\b|delist|recall|halts? (?:trading|production)",
}
_PATTERNS: Final[dict[EventKind, re.Pattern[str]]] = {k: re.compile(v, re.IGNORECASE) for k, v in _HINTS.items()}


@dataclass(frozen=True)
class PreFilterResult:
    """Held tickers named by the item and event kinds hinted at; ``passed`` needs both."""

    tickers: tuple[str, ...]
    kinds: tuple[EventKind, ...]

    @property
    def passed(self) -> bool:
        """The hard gate: at least one held ticker AND at least one event-kind hint."""
        return bool(self.tickers) and bool(self.kinds)


def _mentions(text: str, ticker: str) -> bool:
    """Case-sensitive word match of ``ticker``, or its cashtag. One-letter tickers need the cashtag (``$F``)."""
    if f"${ticker}" in text:
        return True
    if len(ticker) < 2:
        return False
    return re.search(rf"(?<![A-Za-z0-9.]){re.escape(ticker)}(?![A-Za-z0-9])", text) is not None


def prefilter(item: TriageItem, held: Collection[str]) -> PreFilterResult:
    """Apply the hard gate to ``item`` against the held tickers. Deterministic and side-effect free."""
    text = f"{item.title}\n{item.snippet}"
    tickers = tuple(sorted(t for t in held if _mentions(text, t)))
    kinds = tuple(k for k, pattern in _PATTERNS.items() if pattern.search(text))
    return PreFilterResult(tickers, kinds)
