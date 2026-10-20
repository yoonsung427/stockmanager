"""The news stage (AS §7.1): plan -> fetch -> normalise -> boundary -> extract -> validate -> rank -> persist.

Runs outside the portfolio lock (it is network- and LLM-bound). Nothing here writes outside ``FileStore``, and no raw
text reaches a log line, a stat or the rejected file: only reason codes, ids and counts.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta
from typing import Any

from stockmanager.domain.docs import Fact, PortfolioDoc
from stockmanager.domain.ports import NewsFetcher
from stockmanager.news import NewsResult, NewsStats, NewsStatus, Rejection
from stockmanager.news.config import NewsConfig
from stockmanager.news.extract import ExtractContext, Extractor, ExtractorUnavailable, run_extraction
from stockmanager.news.fetchers.base import NewsUnavailable
from stockmanager.news.normalise import QueryResult, normalise
from stockmanager.news.plan import NewsQuery, build_plan
from stockmanager.news.rank import FactEntry, rank_and_cap
from stockmanager.news.ratelimit import CircuitBreaker
from stockmanager.news.raw import RawItem
from stockmanager.news.validate import validate_candidate
from stockmanager.store import FileStore

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class _FetchStage:
    """Fetches every planned query through the fallback chain, in parallel, bounded by the stage deadline."""

    def __init__(self, fetchers: Sequence[NewsFetcher], config: NewsConfig, today: date) -> None:
        self._config = config
        self._today = today
        self._by_name = {f.name: f for f in fetchers}
        chain = [n for n in config.fallback_chain if n in self._by_name]
        self._breakers = {n: CircuitBreaker(config.breaker_failures_to_open) for n in chain}
        self._slots = {n: threading.BoundedSemaphore(config.sources[n].max_concurrency) for n in chain}
        self._lock = threading.Lock()
        self.calls: Counter[str] = Counter()
        self.failures: Counter[str] = Counter()

    @property
    def breaker_opened(self) -> bool:
        """True if any adapter's breaker opened during the stage."""
        return any(b.is_open for b in self._breakers.values())

    def run(self, plan: Sequence[NewsQuery]) -> tuple[list[QueryResult], list[NewsQuery], int]:
        """Return the successful results (plan order), the failed queries and how many hit the stage deadline."""
        workers = max(1, min(len(plan), sum(self._config.sources[n].max_concurrency for n in self._breakers)))
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fetch")
        futures = [pool.submit(self._query, i, q) for i, q in enumerate(plan)]
        done, _ = wait(futures, timeout=self._config.fetch_stage_deadline_s)
        pool.shutdown(wait=False, cancel_futures=True)
        results: list[QueryResult] = []
        failed: list[NewsQuery] = []
        timed_out = 0
        for query, fut in zip(plan, futures, strict=True):
            if fut not in done:
                failed.append(query)
                timed_out += 1
            elif (result := fut.result()) is None:
                failed.append(query)
            else:
                results.append(result)
        return results, failed, timed_out

    def _query(self, index: int, query: NewsQuery) -> QueryResult | None:
        held: QueryResult | None = None  # a stale-looking benchmark result, kept in case nothing better turns up
        for name in self._config.fallback_chain:
            fetcher = self._by_name.get(name)
            breaker = self._breakers.get(name)
            if fetcher is None or breaker is None or breaker.is_open:
                continue
            if query.kind not in self._config.sources[name].kinds:
                continue
            with self._slots[name]:
                with self._lock:
                    self.calls[name] += 1
                try:
                    items = fetcher.fetch(query)
                except NewsUnavailable as e:
                    self._failed(name, e.kind)
                    continue
                except Exception:  # an adapter bug must cost one query, not the stage
                    log.exception("news adapter %s raised unexpectedly", name)
                    self._failed(name, "error")
                    continue
                breaker.record_success()
            if query.kind == "benchmark" and self._stale_feed(items):
                log.warning("news adapter %s looks stale for the benchmark query", name)
                held = held or QueryResult(index, query, name, items)
                continue
            return QueryResult(index, query, name, items)
        return held

    def _failed(self, name: str, kind: str) -> None:
        with self._lock:
            self.failures[name] += 1
        if kind == "auth":
            self._breakers[name].trip()
        elif kind == "error":
            self._breakers[name].record_failure()

    def _stale_feed(self, items: Sequence[RawItem]) -> bool:
        """Benchmark only (news-sources §6.3): a 200 whose newest dated item is older than ``stale_feed_days``."""
        dated = [i.published for i in items if i.published is not None]
        return bool(dated) and max(dated) < self._today - timedelta(days=self._config.stale_feed_days)


def run_news_stage(
    cycle_id: str,
    portfolio: PortfolioDoc,
    config: NewsConfig,
    clock: Callable[[], datetime],
    fetchers: Sequence[NewsFetcher],
    extractor: Extractor,
    *,
    store: FileStore,
    last_macro_success: date | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> NewsResult:
    """Run the whole stage and persist ``extracted-facts/<cycle_id>.{json,rejected.jsonl}`` before returning.

    ``news_status`` is ``unavailable`` when every query failed or the extractor is down (no proposals this cycle),
    ``degraded`` when some query failed on every adapter, a breaker opened or extraction had infrastructure failures,
    and ``ok`` otherwise (zero facts with every query answered is a quiet week, still ``ok``).
    """
    now = clock()
    today = now.date()
    stats = NewsStats()
    rejections: list[Rejection] = []
    uncovered: list[str] = []
    prepared_count = 0
    facts: list[Fact] = []
    status: NewsStatus = "ok"

    def finish() -> NewsResult:
        for r in rejections:
            stats.rejected_reasons[r.reason] = stats.rejected_reasons.get(r.reason, 0) + 1
        result = NewsResult(
            cycle_id=cycle_id,
            news_status=status,
            facts=[] if status == "unavailable" else facts,
            uncovered_tickers=uncovered,
            stats=stats,
        )
        doc: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "cycle_id": cycle_id,
            "extractor_model": config.extractor_model,
            "fetched": prepared_count,
            "extracted": len(result.facts),
            "rejected": len(rejections),
            "news_status": status,
            "uncovered_tickers": uncovered,
            "stats": stats.model_dump(mode="json"),
            "facts": [f.model_dump(mode="json") for f in result.facts],
        }
        rows = [
            {
                "cycle_id": cycle_id,
                "ts": now.isoformat(),
                "stage": r.stage,
                "reason": r.reason,
                "item_id": r.item_id,
                "adapter": r.adapter,
            }
            for r in rejections
        ]
        store.save_extracted_facts(cycle_id, doc, rows)
        log.info(
            "news %s: status=%s facts=%d rejected=%d uncovered=%d",
            cycle_id,
            status,
            len(result.facts),
            len(rejections),
            len(uncovered),
        )
        return result

    try:  # prompt integrity is checked before any fetch result is paid for
        extractor.verify()
    except ExtractorUnavailable as e:
        log.error("extractor unavailable: %s", e)
        status = "unavailable"
        return finish()

    holdings = sorted({p.ticker for p in portfolio.positions})
    relevance = config.relevance_set(holdings)
    plan = build_plan(portfolio, config, today, last_macro_success)

    # fetch
    fetch = _FetchStage(fetchers, config, today)
    results, failed, timed_out = fetch.run(plan)
    stats.adapter_calls = dict(fetch.calls)
    stats.adapter_failures = dict(fetch.failures)
    stats.fetched_items = sum(len(r.items) for r in results)
    uncovered = list(dict.fromkeys(q.ticker for q in failed))
    rejections.extend(Rejection("fetch", "fetch_deadline") for _ in range(timed_out))
    if not results:
        status = "unavailable"
        return finish()

    # normalise, then the injection boundary
    chain_priority = {n: i for i, n in enumerate(config.fallback_chain)}
    normalised = normalise(results, config, today, chain_priority)
    rejections.extend(normalised.rejections)
    prepared_count = len(normalised.items)

    ctx = ExtractContext(today, config.benchmark, tuple(sorted(relevance)))
    outcome = run_extraction(extractor, normalised.items, ctx, config, monotonic)
    rejections.extend(outcome.rejections)
    if outcome.down:
        status = "unavailable"
        return finish()

    # validate
    entries: list[FactEntry] = []
    for item in normalised.items:
        for candidate in outcome.candidates.get(item.item_id, []):
            stats.extracted_candidates += 1
            verdict = validate_candidate(candidate, item, relevance, today)
            if isinstance(verdict, Fact):
                entries.append(FactEntry(verdict, item.item_id, item.adapter))
            else:
                rejections.append(Rejection("validate", verdict, item.item_id, item.adapter))
    stats.validated_facts = len(entries)

    # rank and cap
    ranked = rank_and_cap(entries, config)
    rejections.extend(ranked.rejections)
    facts = ranked.facts

    degraded = (
        bool(uncovered)
        or fetch.breaker_opened
        or outcome.infra_failures > 0
        or any(r.reason == "extract_deadline" for r in outcome.rejections)
    )
    status = "degraded" if degraded else "ok"
    return finish()
