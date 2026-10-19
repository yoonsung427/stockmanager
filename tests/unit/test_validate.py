"""Quote validation edge cases."""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from stockmanager.domain.docs import PriceQuote
from stockmanager.pricing.errors import MixedSourceError, PriceError
from stockmanager.pricing.validate import require_single_source, validate_quote

NOW = datetime(2026, 10, 16, 14, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


def quote(price: str, source: str = "yahoo", ticker: str = "AAPL") -> PriceQuote:
    return PriceQuote(ticker, date(2026, 10, 16), "close", Decimal(price), source, NOW)  # type: ignore[arg-type]


def test_valid_quote_is_returned_unchanged() -> None:
    q = quote("263.2175")
    assert validate_quote(q) is q


@pytest.mark.parametrize("price", ["0", "-0", "-1.5", "-0.0001"])
def test_non_positive_rejected(price: str) -> None:
    with pytest.raises(PriceError) as e:
        validate_quote(quote(price))
    assert e.value.kind == "non_positive"


@pytest.mark.parametrize("price", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_non_finite_rejected(price: str) -> None:
    with pytest.raises(PriceError) as e:
        validate_quote(quote(price))
    assert e.value.kind == "non_finite"


def test_tiny_positive_price_is_valid() -> None:
    validate_quote(quote("0.0001"))


def test_single_source() -> None:
    assert require_single_source([quote("1"), quote("2", ticker="MSFT")]) == "yahoo"


def test_mixed_sources_rejected() -> None:
    with pytest.raises(MixedSourceError):
        require_single_source([quote("1"), quote("2", source="finnhub")])


def test_no_quotes_rejected() -> None:
    with pytest.raises(MixedSourceError):
        require_single_source([])
