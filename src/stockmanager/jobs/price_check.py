"""Price-threshold alert (runs shortly after the open on trading days).

Compares each holding's opening price with the previous close in ``state/last_close.json`` (rolled forward by the
digest job). A holding that has moved at least ``summary.price_threshold_pct`` either way gets recent news, a
summary and one outbox alert. A ticker with no stored previous close is skipped: there is nothing to compare to.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from decimal import Decimal

from stockmanager.config.model import Config
from stockmanager.domain.ports import PriceProvider
from stockmanager.jobs.common import (
    NewsLookup,
    SummaryGen,
    et_date,
    holdings,
    load_last_close,
    move_pct,
    open_prices,
    position_info,
    safe_news,
)
from stockmanager.llm.summary import SummaryError
from stockmanager.notify.deliver import Channel, deliver_pending
from stockmanager.notify.outbox import enqueue, raised_dedupe_keys
from stockmanager.notify.templates import summary_alert
from stockmanager.store import FileStore
from stockmanager.store.lock import Clock

log = logging.getLogger(__name__)


def run_price_check(
    store: FileStore,
    config: Config,
    price_provider: PriceProvider,
    summary_gen: SummaryGen,
    channels: Sequence[Channel],
    clock: Clock,
    *,
    price_fallback: PriceProvider | None = None,
    news_lookup: NewsLookup,
) -> list[str]:
    """Enqueue one alert per holding whose open is at or beyond the threshold; return the summaries posted.

    Opens come from one source (primary, else fallback). If neither can price every holding the job raises
    ``ValuationUnavailable`` and leaves everything unchanged, so the scheduler retries. A ticker already alerted
    for today (pending, sent or dead-lettered) is not alerted again. If the model is unavailable the alert still
    goes out with a plain-numbers message: the price move is the news, the prose is a bonus.
    """
    if not config.summary.alert_on_threshold or not store.portfolio_exists():
        return []
    portfolio = store.load_portfolio()
    if portfolio.status != "LIVE":
        return []
    previous = load_last_close(store.root)
    held = [h for h in holdings(portfolio) if h.ticker in previous]
    if not held:
        log.info("price check: no previous closes on file, nothing to compare")
        return []

    today = et_date(clock())
    opens = open_prices([h.ticker for h in held], today, price_provider, price_fallback)
    threshold = Decimal(str(config.summary.price_threshold_pct))
    already = raised_dedupe_keys(store.root)

    posted: list[str] = []
    for h in held:
        prev, now_price = previous[h.ticker], opens[h.ticker]
        change = move_pct(now_price, prev)
        key = f"price_move:{h.ticker}:{today.isoformat()}"
        if change is None or abs(change) < threshold or key in already:
            continue
        info = position_info(h, now_price, today, prev)
        try:
            text = summary_gen(h.ticker, safe_news(news_lookup, h.ticker, today), info, today)
        except SummaryError as e:
            log.warning("price check: no summary for %s, sending the numbers only: %s", h.ticker, e)
            text = f"{h.ticker} opened {change:+.2f}% vs the previous close ({prev:.2f} -> {now_price:.2f})."
        enqueue(summary_alert(h.ticker, text, severity="alert", dedupe_key=key), store)
        posted.append(text)
    if posted and channels:
        try:
            deliver_pending(store, channels, clock, time_budget=20.0, visible_channels=config.ops.visible_channels)
        except Exception:
            log.exception("price check: immediate delivery failed; the alerts stay queued")
    return posted
