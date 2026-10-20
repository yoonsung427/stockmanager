"""News pipeline (AS §7): plan -> fetch -> normalise -> INJECTION BOUNDARY -> extract -> validate -> rank -> persist.

Everything left of the boundary handles untrusted text; everything right of it handles typed records only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from stockmanager.domain.docs import Fact

NewsStatus = Literal["ok", "degraded", "unavailable"]

__all__ = ["NewsResult", "NewsStats", "NewsStatus", "Rejection"]


@dataclass(frozen=True)
class Rejection:
    """One dropped item or candidate. Carries a reason code and ids only, never news text (AS §7.1)."""

    stage: Literal["fetch", "normalise", "extract", "validate", "rank"]
    reason: str
    item_id: str | None = None
    adapter: str | None = None


class NewsStats(BaseModel):
    """Counters per stage, per adapter and per reason code."""

    fetched_items: int = 0
    extracted_candidates: int = 0
    validated_facts: int = 0
    adapter_calls: dict[str, int] = Field(default_factory=dict)
    adapter_failures: dict[str, int] = Field(default_factory=dict)
    rejected_reasons: dict[str, int] = Field(default_factory=dict)


class NewsResult(BaseModel):
    """What the news stage hands to the Reasoner input builder (typed records only)."""

    cycle_id: str
    news_status: NewsStatus
    facts: list[Fact]
    uncovered_tickers: list[str]
    stats: NewsStats
