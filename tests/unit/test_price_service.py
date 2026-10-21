"""PriceService: primary -> fallback, one source per valuation, fail closed."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Sequence
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from stockmanager.domain.docs import PriceQuote, PriceTable
from stockmanager.pricing.errors import MixedSourceError, PriceError, PriceErrorKind, ValuationUnavailable
from stockmanager.pricing.service import PriceService

D = date(2026, 10, 16)
NOW = datetime(2026, 10, 16, 14, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
Kind = Literal["open", "close"]


class Fake:
    """Scripted provider: ``prices`` per ticker, ``fail`` maps ticker -> error kind."""

    def __init__(
        self, name: Literal["yahoo", "finnhub"], prices: dict[str, str], fail: dict[str, PriceErrorKind] | None = None
    ) -> None:
        self.name = name
        self.prices = prices
        self.fail = fail or {}
        self.calls: list[tuple[str, Kind]] = []

    def get_price(self, ticker: str, session_date: date, kind: Kind) -> PriceQuote:
        self.calls.append((ticker, kind))
        if ticker in self.fail:
            raise PriceError(self.fail[ticker], ticker)
        return PriceQuote(ticker, session_date, kind, Decimal(self.prices[ticker]), self.name, NOW)

    def prime(self, tickers: Sequence[str], session_date: date, kind: Kind) -> dict[str, Exception]:
        return {}


PRICES = {"AAPL": "100", "MSFT": "200", "SPY": "300"}


def test_primary_used_when_it_prices_everything() -> None:
    y, f = Fake("yahoo", PRICES), Fake("finnhub", PRICES)
    table = PriceService().valuation(["AAPL", "MSFT"], D, y, f)
    assert table.source == "yahoo" and table.session_date == D
    assert {t: q.price for t, q in table.quotes.items()} == {"AAPL": Decimal(100), "MSFT": Decimal(200)}
    assert f.calls == []
    assert {k for _, k in y.calls} == {"close"}


def test_any_primary_failure_redoes_all_tickers_on_fallback() -> None:
    y, f = Fake("yahoo", PRICES, {"MSFT": "timeout"}), Fake("finnhub", PRICES)
    table = PriceService().valuation(["AAPL", "MSFT", "SPY"], D, y, f)
    assert table.source == "finnhub"
    assert {q.source for q in table.quotes.values()} == {"finnhub"}  # AAPL is not patched in from yahoo
    assert [t for t, _ in f.calls] == ["AAPL", "MSFT", "SPY"]


def test_both_sources_failing_fails_closed() -> None:
    y, f = Fake("yahoo", PRICES, {"AAPL": "http_error"}), Fake("finnhub", PRICES, {"SPY": "stale_date"})
    with pytest.raises(ValuationUnavailable) as e:
        PriceService().valuation(["AAPL", "SPY"], D, y, f)
    assert set(e.value.failures) == {"yahoo", "finnhub"}


def test_invalid_price_counts_as_failure() -> None:
    y, f = Fake("yahoo", {**PRICES, "AAPL": "0"}), Fake("finnhub", PRICES)
    assert PriceService().valuation(["AAPL"], D, y, f).source == "finnhub"


def test_provider_returning_wrong_ticker_or_source_is_rejected() -> None:
    class Liar(Fake):
        def get_price(self, ticker: str, session_date: date, kind: Kind) -> PriceQuote:
            return PriceQuote("OTHER", session_date, kind, Decimal(1), self.name, NOW)

    f = Fake("finnhub", PRICES)
    assert PriceService().valuation(["AAPL"], D, Liar("yahoo", PRICES), f).source == "finnhub"


def test_duplicates_are_priced_once_and_empty_is_an_error() -> None:
    y, f = Fake("yahoo", PRICES), Fake("finnhub", PRICES)
    PriceService().valuation(["AAPL", "AAPL"], D, y, f)
    assert len(y.calls) == 1
    with pytest.raises(ValueError):
        PriceService().valuation([], D, y, f)


def test_fill_prices_uses_open_and_one_source_per_group() -> None:
    y, f = Fake("yahoo", PRICES, {"MSFT": "malformed"}), Fake("finnhub", PRICES)
    fills = PriceService().fill_prices([["AAPL"], ["MSFT", "SPY"]], D, y, f)
    assert {t: q.source for t, q in fills.items()} == {"AAPL": "yahoo", "MSFT": "finnhub", "SPY": "finnhub"}
    assert {q.kind for q in fills.values()} == {"open"}


def test_fill_prices_all_or_nothing_across_groups() -> None:
    y, f = Fake("yahoo", PRICES, {"SPY": "timeout"}), Fake("finnhub", PRICES, {"SPY": "timeout"})
    with pytest.raises(ValuationUnavailable):
        PriceService().fill_prices([["AAPL"], ["SPY"]], D, y, f)


def test_fill_prices_rejects_ticker_in_two_groups() -> None:
    y, f = Fake("yahoo", PRICES), Fake("finnhub", PRICES)
    with pytest.raises(ValueError):
        PriceService().fill_prices([["AAPL", "MSFT"], ["MSFT"]], D, y, f)


def q(ticker: str, source: Literal["yahoo", "finnhub"], d: date = D) -> PriceQuote:
    return PriceQuote(ticker, d, "close", Decimal(1), source, NOW)


def test_price_table_rejects_mixed_source() -> None:
    with pytest.raises(MixedSourceError):
        PriceTable(source="yahoo", session_date=D, quotes={"A": q("A", "yahoo"), "B": q("B", "finnhub")})


def test_price_table_rejects_mixed_session() -> None:
    with pytest.raises(MixedSourceError):
        PriceTable(source="yahoo", session_date=D, quotes={"A": q("A", "yahoo", date(2026, 10, 15))})


def test_price_table_rejects_misfiled_quote() -> None:
    with pytest.raises(ValidationError):
        PriceTable(source="yahoo", session_date=D, quotes={"B": q("A", "yahoo")})
