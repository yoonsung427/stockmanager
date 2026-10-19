"""Smoke tests for domain models and money helpers."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from stockmanager.domain.docs import PortfolioDoc, Quote
from stockmanager.domain.money import q4

PT = ZoneInfo("America/Los_Angeles")


def test_q4_half_up() -> None:
    assert q4("1.00005") == Decimal("1.0001")
    assert q4(Decimal("2.5")) == Decimal("2.5000")


def test_q4_rejects_float() -> None:
    with pytest.raises(TypeError):
        q4(1.5)  # type: ignore[arg-type]


def test_quote_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        Quote.model_validate(
            {"ticker": "AAPL", "price": Decimal(1), "source": "yahoo", "timestamp": datetime.now(PT), "x": 1}
        )


def test_portfolio_canonical_json_roundtrip() -> None:
    doc = PortfolioDoc(start_cash=Decimal(10000), cash=Decimal(10000), status="LIVE")
    text = doc.to_canonical_json()
    assert text.endswith("\n")
    assert '"cash": "10000.0000"' in text
    assert PortfolioDoc.model_validate_json(text) == doc
