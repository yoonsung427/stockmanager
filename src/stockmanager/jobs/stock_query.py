"""Interactive stock queries from Discord: "what's going on with MSFT?", "Microsoft news", "check MSFT".

Only reached after the command parser rejected a message, so real commands always win. The answer reveals position
sizes and P&L, so it is limited to ``discord.allowed_user_ids``; anyone else gets nothing here and the bot's usual
"unrecognized command" reply. Only current holdings are answered: the name is matched against held tickers directly
or, for a company name, through a symbol search whose results are intersected with the holdings.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal

from stockmanager.commands.handlers import CommandContext
from stockmanager.jobs.common import (
    NewsLookup,
    SummaryGen,
    TradingCalendar,
    close_prices,
    et_date,
    holdings,
    latest_closed_session,
    position_info,
    safe_news,
)
from stockmanager.llm.summary import SummaryError
from stockmanager.pricing.errors import PriceError, ValuationUnavailable

log = logging.getLogger(__name__)

#: Anchored on purpose: unanchored, "any good news today?" would be read as a query about "good".
_TAIL = r"\s*[?!.]*\s*$"
STOCK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*what['’]?s?\s+going\s+on\s+with\s+(.+?)" + _TAIL, re.IGNORECASE),
    re.compile(r"^\s*(.+?)\s+news" + _TAIL, re.IGNORECASE),
    re.compile(r"^\s*check\s+(.+?)" + _TAIL, re.IGNORECASE),
)
_TICKER_LIKE = re.compile(r"^[A-Z][A-Z.\-]{0,5}$")
_NAME_OK = re.compile(r"^[\w.&\- ]{1,40}$")
MAX_REPLY_CHARS = 1900  # Discord's message limit is 2000

#: ``name -> candidate tickers`` (best first); the composition root supplies the yfinance-backed one.
AliasResolver = Callable[[str], list[str]]


def extract_candidate(text: str) -> str | None:
    """The ticker or company name a stock-query message asks about, or ``None`` if it is not one."""
    for pattern in STOCK_PATTERNS:
        m = pattern.match(text)
        if m and _NAME_OK.match(m.group(1)):
            return m.group(1).strip()
    return None


class StockQuery:
    """Answers stock queries about holdings with an AI summary. Callable: ``(text, user_id) -> reply or None``."""

    def __init__(
        self,
        ctx: CommandContext,
        calendar: TradingCalendar,
        news_lookup: NewsLookup,
        summary_gen: SummaryGen,
        resolve_alias: AliasResolver,
    ) -> None:
        self._ctx = ctx
        self._calendar = calendar
        self._news = news_lookup
        self._summary = summary_gen
        self._resolve_alias = resolve_alias
        self._aliases: dict[str, list[str]] = {}

    def __call__(self, text: str, user_id: str) -> str | None:
        """``None`` means "not a stock query for you": the caller keeps its own reply. Blocking; run off the loop."""
        cfg = self._ctx.cfg
        if not cfg.summary.enabled or user_id not in cfg.discord.allowed_user_ids:
            return None
        candidate = extract_candidate(text)
        if candidate is None or not self._ctx.store.portfolio_exists():
            return None
        held = {h.ticker: h for h in holdings(self._ctx.store.load_portfolio())}
        ticker = self._resolve(candidate, set(held))
        if ticker is None:
            # Only a ticker-looking word earns a reply; "good news" and the like fall through to the usual rejection.
            return f"⚠️ {candidate} is not in the portfolio." if _TICKER_LIKE.match(candidate) else None
        now = self._ctx.clock()
        price, price_date = self._latest_close(ticker, now)
        info = position_info(held[ticker], price, price_date, None)
        session = et_date(now)
        try:
            summary = self._summary(ticker, safe_news(self._news, ticker, session), info, session)
        except SummaryError as e:
            log.warning("stock query: no summary for %s: %s", ticker, e)
            return f"⚠️ Couldn't generate a summary for {ticker} right now. Try again in a minute."
        return f"**{ticker}** — {summary}"[:MAX_REPLY_CHARS]

    def _resolve(self, candidate: str, held: set[str]) -> str | None:
        upper = candidate.upper()
        if upper in held:
            return upper
        if upper not in self._aliases:
            self._aliases[upper] = self._resolve_alias(candidate)
        return next((s for s in self._aliases[upper] if s in held), None)

    def _latest_close(self, ticker: str, now: datetime) -> tuple[Decimal | None, date | None]:
        """The last completed session's close, or ``(None, None)`` if it cannot be priced (fail closed)."""
        try:
            session = latest_closed_session(self._calendar, now)
            prices = close_prices([ticker], session, self._ctx.price_provider, self._ctx.price_fallback)
        except (ValueError, PriceError, ValuationUnavailable) as e:
            log.info("stock query: no close for %s: %s", ticker, e)
            return None, None
        return prices[ticker], session
