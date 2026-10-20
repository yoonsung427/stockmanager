"""Ranking and caps on the evidence (AS §7.6, news-sources §4.5)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from stockmanager.domain.docs import Fact
from stockmanager.news import Rejection
from stockmanager.news.config import NewsConfig


@dataclass(frozen=True)
class FactEntry:
    """A validated fact with the id of the item it came from (the tie-break key; ``Fact`` does not carry it)."""

    fact: Fact
    item_id: str
    adapter: str | None = None


@dataclass(frozen=True)
class Ranked:
    """Output of ``rank_and_cap``: the surviving facts, newest first, and what was dropped."""

    facts: list[Fact]
    rejections: list[Rejection]


def rank_and_cap(entries: Sequence[FactEntry], config: NewsConfig) -> Ranked:
    """Sort newest first, collapse duplicates, then cap per (ticker, domain), per ticker and in total."""
    ordered = sorted(entries, key=lambda e: (-e.fact.date.toordinal(), e.item_id, e.fact.fact_id))
    rejections: list[Rejection] = []

    def drop(entry: FactEntry, reason: str) -> None:
        rejections.append(Rejection("rank", reason, entry.item_id, entry.adapter))

    seen: set[tuple[str, str, object, str]] = set()
    unique: list[FactEntry] = []
    for e in ordered:
        key = (e.fact.ticker, e.fact.event, e.fact.date, e.fact.description)
        if key in seen:
            drop(e, "dup_fact")
        else:
            seen.add(key)
            unique.append(e)

    per_domain: Counter[tuple[str, str]] = Counter()
    stage1: list[FactEntry] = []
    for e in unique:
        domain_key = (e.fact.ticker, e.fact.source_domain)
        if per_domain[domain_key] >= config.max_facts_per_ticker_domain:
            drop(e, "cap_ticker_domain")
        else:
            per_domain[domain_key] += 1
            stage1.append(e)

    per_ticker: Counter[str] = Counter()
    stage2: list[FactEntry] = []
    for e in stage1:
        if per_ticker[e.fact.ticker] >= config.max_facts_per_ticker:
            drop(e, "cap_ticker")
        else:
            per_ticker[e.fact.ticker] += 1
            stage2.append(e)

    for e in stage2[config.max_facts_total :]:
        drop(e, "cap_total")
    return Ranked([e.fact for e in stage2[: config.max_facts_total]], rejections)
