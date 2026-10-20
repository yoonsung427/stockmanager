"""Ranking and caps (AS §7.6): newest first, duplicates collapsed, <=3 per (ticker, domain), <=15 per ticker, <=150 total."""

from datetime import date, timedelta

from stockmanager.domain.docs import Fact, NewsEvent
from stockmanager.news.config import NewsConfig
from stockmanager.news.rank import FactEntry, rank_and_cap

DAY = date(2026, 10, 19)


def fact(
    n: int,
    ticker: str = "AAPL",
    domain: str = "a.com",
    age: int = 0,
    event: NewsEvent = "other",
    desc: str | None = None,
) -> Fact:
    return Fact(
        fact_id=f"f_{ticker}{domain}{n}",
        ticker=ticker,
        event=event,
        description=desc or f"d{n}",
        date=DAY - timedelta(days=age),
        source_url=f"https://{domain}/{n}",
        source_domain=domain,
        display_title="t",
    )


def entries(*facts: Fact) -> list[FactEntry]:
    return [FactEntry(f, f"item{i:04d}") for i, f in enumerate(facts)]


def reasons(rejections: list) -> dict[str, int]:  # type: ignore[type-arg]
    out: dict[str, int] = {}
    for r in rejections:
        out[r.reason] = out.get(r.reason, 0) + 1
    return out


def test_newest_first_with_deterministic_tie_break() -> None:
    old, new, tie_a, tie_b = (
        fact(1, age=5, domain="o.com"),
        fact(2, age=0, domain="n.com"),
        fact(3, age=2, domain="p.com"),
        fact(4, age=2, domain="q.com"),
    )
    shuffled = [FactEntry(tie_b, "z"), FactEntry(old, "y"), FactEntry(tie_a, "a"), FactEntry(new, "m")]
    out = rank_and_cap(shuffled, NewsConfig()).facts
    assert out == [new, tie_a, tie_b, old]
    assert rank_and_cap(list(reversed(shuffled)), NewsConfig()).facts == out


def test_at_most_three_per_ticker_and_domain_keeps_newest() -> None:
    facts = [fact(n, age=n) for n in range(5)]
    out = rank_and_cap(entries(*facts), NewsConfig())
    assert [f.fact_id for f in out.facts] == [f.fact_id for f in facts[:3]]
    assert reasons(out.rejections) == {"cap_ticker_domain": 2}


def test_same_domain_different_tickers_are_separate_buckets() -> None:
    facts = [fact(n, ticker=t) for t in ("AAPL", "MSFT") for n in range(3)]
    assert len(rank_and_cap(entries(*facts), NewsConfig()).facts) == 6


def test_at_most_fifteen_per_ticker() -> None:
    facts = [fact(n, domain=f"d{n}.com", age=n) for n in range(20)]
    out = rank_and_cap(entries(*facts), NewsConfig())
    assert len(out.facts) == 15 and reasons(out.rejections) == {"cap_ticker": 5}
    assert out.facts[-1].fact_id == facts[14].fact_id  # the oldest five were cut


def test_at_most_150_total() -> None:
    facts = [
        fact(0, ticker=f"T{t}", domain=f"d{n}.com", age=t % 7, desc=f"x{t}-{n}") for t in range(20) for n in range(10)
    ]
    out = rank_and_cap(entries(*facts), NewsConfig())
    assert len(out.facts) == 150 and reasons(out.rejections) == {"cap_total": 50}
    kept_ids = {f.fact_id for f in out.facts}
    dropped_dates = [f.date for f in facts if f.fact_id not in kept_ids]
    assert max(dropped_dates) <= min(f.date for f in out.facts)  # newest-first: only the oldest were cut


def test_caps_are_configurable() -> None:
    cfg = NewsConfig(max_facts_per_ticker_domain=1, max_facts_per_ticker=2, max_facts_total=3)
    facts = [fact(n, domain=f"d{n % 2}.com", age=n) for n in range(6)] + [
        fact(9, ticker="MSFT"),
        fact(10, ticker="MSFT", domain="b.com"),
    ]
    out = rank_and_cap(entries(*facts), cfg)
    assert len(out.facts) == 3


def test_duplicates_collapse_before_capping() -> None:
    dups = [fact(n, desc="same", event="earnings") for n in range(5)]  # identical ticker/event/date/description
    others = [fact(10 + n, domain="b.com") for n in range(3)]
    out = rank_and_cap(entries(*dups, *others), NewsConfig())
    assert reasons(out.rejections) == {"dup_fact": 4}  # not cap_ticker_domain: duplicates did not use up slots
    assert len(out.facts) == 4


def test_rejections_carry_ids_not_text() -> None:
    out = rank_and_cap(entries(*[fact(n) for n in range(4)]), NewsConfig())
    assert out.rejections[0].stage == "rank" and out.rejections[0].item_id == "item0003"
