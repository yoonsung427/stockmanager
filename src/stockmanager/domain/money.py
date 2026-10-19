"""Decimal helpers. Money and shares are 4 dp, rounded ROUND_HALF_UP everywhere."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

__all__ = ["Q4", "ROUND_HALF_UP", "q4"]

Q4 = Decimal("0.0001")


def q4(value: Decimal | str | int) -> Decimal:
    """Quantize to 4 decimal places using ROUND_HALF_UP. Floats are rejected to avoid binary error."""
    if isinstance(value, float):
        raise TypeError("q4() does not accept float; pass Decimal or str")
    return Decimal(value).quantize(Q4, rounding=ROUND_HALF_UP)
