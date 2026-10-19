"""Price errors."""

from __future__ import annotations

from typing import Literal

PriceErrorKind = Literal[
    "timeout", "http_error", "malformed", "non_positive", "non_finite", "stale_date", "incomplete_session", "missing"
]


class PriceError(Exception):
    """A provider could not produce a trustworthy price."""

    def __init__(self, kind: PriceErrorKind, ticker: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {ticker}: {detail}" if detail else f"{kind}: {ticker}")
        self.kind = kind
        self.ticker = ticker
        self.detail = detail


class ValuationUnavailable(Exception):
    """Neither primary nor fallback could price every ticker; callers must fail closed."""

    def __init__(self, failures: dict[str, Exception]) -> None:
        super().__init__("valuation unavailable: " + "; ".join(f"{k}: {v}" for k, v in failures.items()))
        self.failures = failures


class MixedSourceError(Exception):
    """A price table or paired fill contained quotes from more than one source."""
