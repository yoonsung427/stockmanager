"""Daily digest (runs after the close on trading days): one outbox alert with a summary per notable holding.

A holding is *notable* if it moved more than 1% against the previous close or has recent news. The job also rolls
``state/last_close.json`` forward to today's closes, because the morning price check (``jobs.price_check``) compares
the open against it; that happens even when ``summary.digest_at_close`` is off.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from stockmanager.config.model import Config
from stockmanager.domain.ports import PriceProvider
from stockmanager.jobs.common import (
    LAST_CLOSE_NAME,
    NewsLookup,
    SummaryGen,
    close_prices,
    dump_last_close,
    et_date,
    holdings,
    load_last_close,
    move_pct,
    position_info,
    safe_news,
)
from stockmanager.llm.summary import SummaryError
from stockmanager.notify.deliver import Channel, deliver_pending
from stockmanager.notify.outbox import prepare
from stockmanager.notify.templates import digest_alert
from stockmanager.outcomes.models import describe, hit_rate
from stockmanager.outcomes.scoring import load_scores
from stockmanager.pricing.errors import PriceError, ValuationUnavailable
from stockmanager.store import FileStore
from stockmanager.store.lock import Clock

log = logging.getLogger(__name__)

NOTABLE_MOVE_PCT = Decimal(1)
FRIDAY = 4


def run_digest(
    store: FileStore,
    config: Config,
    summary_gen: SummaryGen,
    channels: Sequence[Channel],
    clock: Clock,
    *,
    price_provider: PriceProvider,
    price_fallback: PriceProvider | None = None,
    news_lookup: NewsLookup,
) -> list[str]:
    """Build and enqueue today's digest; return the per-holding summaries it contains (``[]`` if none).

    ``price_provider``/``price_fallback`` and ``news_lookup`` are keyword-only additions to the bare job signature:
    the digest cannot judge a price move or a news hit without them. Closes come from one source (primary, else
    fallback). If neither can price everything, the digest still goes out judged on news alone and
    ``last_close.json`` is left as it was (fail closed). Raises ``SummaryError`` when every summary the digest needed
    failed, so the scheduler retries; ``LockBusy`` if the books are busy.
    """
    if not store.portfolio_exists():
        return []
    portfolio = store.load_portfolio()
    if portfolio.status != "LIVE" or not portfolio.positions:
        return []
    held = holdings(portfolio)
    tickers = [h.ticker for h in held]
    today = et_date(clock())

    try:
        closes = close_prices(tickers, today, price_provider, price_fallback)
    except (PriceError, ValuationUnavailable) as e:
        log.warning("digest: no close prices for %s, judging on news alone: %s", today, e)
        closes = {}
    previous = load_last_close(store.root)

    summaries: list[str] = []
    wanted = failed = 0
    for h in held:
        change = move_pct(closes.get(h.ticker), previous.get(h.ticker))
        news = safe_news(news_lookup, h.ticker, today)
        if not (change is not None and abs(change) > NOTABLE_MOVE_PCT) and not news:
            continue
        if not config.summary.digest_at_close:
            continue
        wanted += 1
        info = position_info(h, closes.get(h.ticker), today if h.ticker in closes else None, previous.get(h.ticker))
        try:
            text = summary_gen(h.ticker, news, info, today)
        except SummaryError as e:
            failed += 1
            log.warning("digest: no summary for %s: %s", h.ticker, e)
            continue
        summaries.append(f"**{h.ticker}** — {text}")
    if wanted and failed == wanted:
        raise SummaryError(f"digest: all {wanted} summaries failed")

    lines = [*summaries, *_weekly_outcomes_line(store, today)]
    alert = digest_alert(lines) if lines else None
    with store.transaction("digest") as tx:
        tx.require(status="LIVE")
        if closes:
            tx.stage_state({LAST_CLOSE_NAME: dump_last_close(closes)})
        if alert is not None:
            prepare(alert, clock())
            tx.stage_alerts({f"{alert.alert_id}.json": alert.to_json()})
    if alert is not None and channels:
        _deliver(store, channels, clock, config)
    return summaries


def _weekly_outcomes_line(store: FileStore, today: date) -> list[str]:
    """The hit-rate line (O2), once a week: on the Friday digest, when any trade has been scored."""
    if today.weekday() != FRIDAY:
        return []
    scores = load_scores(store.root)
    return [describe(hit_rate(scores))] if scores else []


def _deliver(store: FileStore, channels: Sequence[Channel], clock: Clock, config: Config) -> None:
    """Push the fresh alert out now rather than waiting for the next outbox pass; failures stay in the outbox."""
    try:
        deliver_pending(store, channels, clock, time_budget=20.0, visible_channels=config.ops.visible_channels)
    except Exception:
        log.exception("digest: immediate delivery failed; the alert stays queued")

