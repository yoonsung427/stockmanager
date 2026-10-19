"""Quote validation."""

from __future__ import annotations

from collections.abc import Sequence

from stockmanager.domain.docs import PriceQuote
from stockmanager.pricing.errors import MixedSourceError, PriceError


def validate_quote(quote: PriceQuote) -> PriceQuote:
    """Reject NaN, infinite and non-positive prices; return the quote unchanged if valid."""
    if not quote.price.is_finite():
        raise PriceError("non_finite", quote.ticker, str(quote.price))
    if quote.price <= 0:
        raise PriceError("non_positive", quote.ticker, str(quote.price))
    return quote


def require_single_source(quotes: Sequence[PriceQuote]) -> str:
    """Return the one source shared by all quotes, or raise ``MixedSourceError`` (also for no quotes)."""
    sources = {q.source for q in quotes}
    if len(sources) != 1:
        raise MixedSourceError(f"sources: {sorted(sources)}")
    return sources.pop()
