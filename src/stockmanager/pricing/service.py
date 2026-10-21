"""PriceService: one source per valuation, fail closed."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date
from typing import Literal

from stockmanager.domain.docs import PriceQuote, PriceTable
from stockmanager.domain.ports import PriceProvider
from stockmanager.pricing.errors import PriceError, ValuationUnavailable
from stockmanager.pricing.validate import validate_quote

log = logging.getLogger(__name__)


class PriceService:
    """Primary-then-fallback pricing. Stateless: the providers are passed per call."""

    def valuation(
        self, tickers: Sequence[str], session_date: date, primary: PriceProvider, fallback: PriceProvider
    ) -> PriceTable:
        """Price every ticker's close from ONE source; if the primary fails for any, redo all on the fallback.

        Raises ``ValuationUnavailable`` if neither source can price everything.
        """
        wanted = _unique(tickers)
        if not wanted:
            raise ValueError("no tickers to value")
        quotes = self._one_source(wanted, session_date, "close", primary, fallback)
        return PriceTable(source=quotes[wanted[0]].source, session_date=session_date, quotes=quotes)

    def fill_prices(
        self, groups: list[list[str]], session_date: date, primary: PriceProvider, fallback: PriceProvider
    ) -> dict[str, PriceQuote]:
        """Price the opening fills per group: one source per group, all-or-nothing across groups.

        Groups may use different sources (TS EDGE-30). If any group is unpriceable nothing is returned.
        """
        seen: set[str] = set()
        for group in groups:
            overlap = seen.intersection(group)
            if overlap:
                raise ValueError(f"ticker in more than one group: {sorted(overlap)}")
            seen.update(group)
        fills: dict[str, PriceQuote] = {}
        for group in groups:
            wanted = _unique(group)
            if wanted:
                fills.update(self._one_source(wanted, session_date, "open", primary, fallback))
        return fills

    def _one_source(
        self,
        tickers: list[str],
        session_date: date,
        kind: Literal["open", "close"],
        primary: PriceProvider,
        fallback: PriceProvider,
    ) -> dict[str, PriceQuote]:
        failures: dict[str, Exception] = {}
        for provider in (primary, fallback):
            try:
                return _price_all(provider, tickers, session_date, kind)
            except PriceError as e:
                failures[provider.name] = e
                log.warning("%s could not price %s for %s: %s", provider.name, kind, session_date, e)
        raise ValuationUnavailable(failures)


def _price_all(
    provider: PriceProvider, tickers: list[str], session_date: date, kind: Literal["open", "close"]
) -> dict[str, PriceQuote]:
    # Use batch pricing if provider supports it (e.g. Yahoo)
    if hasattr(provider, "get_prices_batch"):
        quotes = provider.get_prices_batch(tickers, session_date, kind)
        for ticker, quote in quotes.items():
            validated = validate_quote(quote)
            if (validated.ticker, validated.session_date, validated.kind, validated.source) != (ticker, session_date, kind, provider.name):
                raise PriceError("malformed", ticker, f"{provider.name} returned a mismatched quote: {validated}")
        return quotes
    # Fall back to per-ticker pricing
    quotes: dict[str, PriceQuote] = {}
    for ticker in tickers:
        quote = validate_quote(provider.get_price(ticker, session_date, kind))
        if (quote.ticker, quote.session_date, quote.kind, quote.source) != (ticker, session_date, kind, provider.name):
            raise PriceError("malformed", ticker, f"{provider.name} returned a mismatched quote: {quote}")
        quotes[ticker] = quote
    return quotes


def _unique(tickers: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(tickers))
