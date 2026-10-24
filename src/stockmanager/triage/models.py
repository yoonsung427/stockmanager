"""Triage value types (v2 design D1): the item, the event-kind enum, the classifier verdict and the logged decision.

``Decision`` is where "no citation, no escalation" is enforced in code (A5): an escalating decision without a usable
source URL cannot be constructed, so no code path can log or dispatch one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal
from urllib.parse import urlsplit


class EventKind(StrEnum):
    """The closed set of event kinds the hard gate and the classifier speak (A1). Anything else is not an event."""

    EARNINGS = "earnings"
    GUIDANCE = "guidance"
    MERGER_ACQUISITION = "merger_acquisition"
    REGULATORY = "regulatory"
    LEGAL = "legal"
    MANAGEMENT_CHANGE = "management_change"
    ANALYST_RATING = "analyst_rating"
    CAPITAL_RETURN = "capital_return"
    DISTRESS = "distress"


Action = Literal[
    "silent",  # below the hard gate or below the confidence threshold: logged, nobody woken
    "escalate",  # wake now, via the ``triage_escalation`` DueJob
    "defer_digest",  # would escalate, but inside quiet hours: wake at the morning delivery instead (R5)
    "shadow_escalate",  # would have escalated; shadow mode records it and executes nothing
    "blocked_no_citation",  # would escalate, but has no usable source URL (A5)
    "suppressed_duplicate",  # same (ticker, event_hash) already escalated inside the dedupe window (A4)
    "suppressed_cap",  # ``max_escalations_per_day`` reached
    "classifier_down",  # not logged: the item stays in the backlog and is retried on recovery (R4)
]

#: Actions that wake the evaluation path. Each needs a citation.
ESCALATING_ACTIONS: Final[frozenset[str]] = frozenset({"escalate", "defer_digest"})
#: Actions that need a citation because a human will read them as "what triggered this".
CITED_ACTIONS: Final[frozenset[str]] = ESCALATING_ACTIONS | {"shadow_escalate"}


def has_citation(url: str | None) -> bool:
    """True for an absolute http(s) URL with a host. Prices or claims lifted from text are not citations."""
    if not url:
        return False
    parts = urlsplit(url.strip())
    return parts.scheme in ("http", "https") and bool(parts.netloc)


@dataclass(frozen=True)
class TriageItem:
    """One raw fetched item (a ``fetch-log`` row, D8). Untrusted text: only the pre-filter and classifier read it."""

    item_hash: str
    url: str
    title: str
    snippet: str
    fetched_at: datetime
    source: str = ""

    def __repr__(self) -> str:
        """Redacted: no raw text (AS §7.3)."""
        return f"TriageItem(item_hash={self.item_hash!r}, url={self.url!r})"


@dataclass(frozen=True)
class Classification:
    """A classifier verdict. ``event_id``, when the model can name the underlying event, de-duplicates across outlets."""

    event_kind: EventKind
    confidence: float
    event_id: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")


@dataclass(frozen=True)
class Decision:
    """One row of ``triage_decisions`` (D7). ``due_at`` is when an escalating decision's wake becomes due."""

    ts: datetime
    ticker: str
    event_hash: str
    hard_gate: bool
    confidence: float | None
    action: Action
    source_url: str | None
    due_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.action in CITED_ACTIONS and not has_citation(self.source_url):
            raise ValueError(f"action {self.action!r} requires a source URL: no citation, no escalation")
        if self.action in ESCALATING_ACTIONS and self.due_at is None:
            raise ValueError(f"action {self.action!r} requires a due_at")
