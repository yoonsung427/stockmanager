"""Yahoo adapter: bar selection by exchange-timezone date, validation, settle window, retries."""

import copy
import json
import logging
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pytest

from stockmanager.adapters.http import HttpResponse
from stockmanager.adapters.yahoo import YahooPriceProvider
from stockmanager.pricing.errors import PriceError

FIXTURE = Path(__file__).parent.parent / "fixtures" / "prices" / "yahoo_response.json"
PT = ZoneInfo("America/Los_Angeles")
AFTER_CLOSE = datetime(2026, 10, 16, 13, 30, tzinfo=PT)  # 16:30 ET Friday
Kind = Literal["open", "close"]


def load() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


class Transport:
    """Replays queued responses (or exceptions) and records requests."""

    def __init__(self, *responses: HttpResponse | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, Mapping[str, str]]] = []

    def __call__(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        self.requests.append((url, headers))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(doc: dict[str, Any]) -> HttpResponse:
    return HttpResponse(200, json.dumps(doc).encode())


def provider(
    transport: Transport, now: datetime = AFTER_CLOSE, sleeps: list[float] | None = None
) -> YahooPriceProvider:
    return YahooPriceProvider(now=lambda: now, http_get=transport, sleep=(sleeps if sleeps is not None else []).append)


def fetch(doc: dict[str, Any], d: date = date(2026, 10, 16), kind: Kind = "close", now: datetime = AFTER_CLOSE) -> Any:
    return provider(Transport(ok(doc)), now).get_price("AAPL", d, kind)


def mutate(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    doc = load()
    change(doc)
    return doc


def result(doc: dict[str, Any]) -> dict[str, Any]:
    r: dict[str, Any] = doc["chart"]["result"][0]
    return r


def err(doc: dict[str, Any], **kw: Any) -> PriceError:
    with pytest.raises(PriceError) as e:
        fetch(doc, **kw)
    return e.value


def test_close_and_open_come_from_the_dated_bar() -> None:
    c = fetch(load())
    assert (c.price, c.kind, c.source, c.session_date, c.fetched_at) == (
        Decimal("263.2175"),
        "close",
        "yahoo",
        date(2026, 10, 16),
        AFTER_CLOSE,
    )
    assert fetch(load(), d=date(2026, 10, 15), kind="open").price == Decimal("259.0000")
    assert fetch(load(), d=date(2026, 10, 12)).price == Decimal("258.4000")


def test_price_is_not_taken_from_regular_market_price() -> None:
    doc = mutate(lambda d: result(d)["meta"].update(regularMarketPrice=999.0))
    assert fetch(doc, d=date(2026, 10, 13)).price == Decimal("260.0500")


def test_request_url_and_headers() -> None:
    t = Transport(ok(load()))
    provider(t).get_price("aapl", date(2026, 10, 16), "close")
    url, headers = t.requests[0]
    assert url == "https://query2.finance.yahoo.com/v8/finance/chart/AAPL?interval=1d&range=3mo&includePrePost=false"
    assert "Mozilla" in headers["User-Agent"]


def test_bar_date_uses_exchange_timezone_not_utc() -> None:
    # 00:30 UTC on the 17th is 20:30 ET on the 16th: the bar belongs to the 16th.
    def shift(d: dict[str, Any]) -> None:
        result(d)["timestamp"][-1] = int(datetime(2026, 10, 17, 0, 30, tzinfo=ZoneInfo("UTC")).timestamp())

    assert fetch(mutate(shift)).price == Decimal("263.2175")


def test_exchange_timezone_other_than_new_york() -> None:
    # 22:30 UTC on the 16th is 07:30 on the 17th in Tokyo.
    def tokyo(d: dict[str, Any]) -> None:
        result(d)["meta"]["exchangeTimezoneName"] = "Asia/Tokyo"
        result(d)["timestamp"][-1] = int(datetime(2026, 10, 16, 22, 30, tzinfo=ZoneInfo("UTC")).timestamp())

    doc = mutate(tokyo)
    assert fetch(doc, d=date(2026, 10, 17), kind="open").price == Decimal("262.0000")
    assert err(doc, d=date(2026, 10, 16), kind="open").kind == "missing"


def test_null_bar_is_missing() -> None:
    assert err(load(), d=date(2026, 10, 14)).kind == "missing"
    assert err(load(), d=date(2026, 10, 14), kind="open").kind == "missing"


def test_date_after_last_bar_is_stale() -> None:
    now = datetime(2026, 10, 19, 14, 0, tzinfo=PT)
    assert err(load(), d=date(2026, 10, 19), now=now).kind == "stale_date"


def test_date_before_first_bar_or_weekend_gap_is_missing() -> None:
    assert err(load(), d=date(2026, 10, 9)).kind == "missing"


def test_close_before_settle_is_incomplete_without_any_http_call() -> None:
    t = Transport()
    p = provider(t, now=datetime(2026, 10, 16, 13, 19, 59, tzinfo=PT))  # 16:19:59 ET
    with pytest.raises(PriceError) as e:
        p.get_price("AAPL", date(2026, 10, 16), "close")
    assert e.value.kind == "incomplete_session" and t.requests == []


def test_close_exactly_at_settle_is_allowed() -> None:
    assert fetch(load(), now=datetime(2026, 10, 16, 13, 20, tzinfo=PT)).price == Decimal("263.2175")


def test_open_needs_no_settle() -> None:
    now = datetime(2026, 10, 16, 6, 31, tzinfo=PT)
    assert fetch(load(), kind="open", now=now).price == Decimal("262.0000")


@pytest.mark.parametrize(
    "change",
    [
        lambda d: result(d)["meta"].update(currency="EUR"),
        lambda d: result(d)["meta"].update(instrumentType="CRYPTOCURRENCY"),
        lambda d: result(d)["meta"].pop("instrumentType"),
        lambda d: result(d)["meta"].update(exchangeTimezoneName="Mars/Olympus"),
        lambda d: result(d)["meta"].pop("exchangeTimezoneName"),
        lambda d: result(d).pop("timestamp"),
        lambda d: result(d)["indicators"]["quote"][0].update(close=[1.0]),
        lambda d: result(d)["indicators"].pop("quote"),
        lambda d: result(d)["indicators"]["quote"][0].update(close="x"),
        lambda d: result(d)["indicators"]["quote"][0]["close"].__setitem__(4, "263.2"),
        lambda d: d["chart"].update(result=[]),
        lambda d: d["chart"].update(result=None),
        lambda d: d["chart"].update(error={"code": "Not Found"}),
        lambda d: d.pop("chart"),
    ],
)
def test_structural_surprises_are_malformed(change: Callable[[dict[str, Any]], None]) -> None:
    assert err(mutate(change)).kind == "malformed"


def test_etf_is_accepted() -> None:
    doc = mutate(lambda d: result(d)["meta"].update(instrumentType="ETF"))
    assert fetch(doc).price == Decimal("263.2175")


def test_integer_price_is_accepted() -> None:
    doc = mutate(lambda d: result(d)["indicators"]["quote"][0]["close"].__setitem__(4, 263))
    assert fetch(doc).price == Decimal("263.0000")


@pytest.mark.parametrize(("value", "kind"), [(0, "non_positive"), (-5.5, "non_positive")])
def test_bad_prices(value: float, kind: str) -> None:
    doc = mutate(lambda d: result(d)["indicators"]["quote"][0]["close"].__setitem__(4, value))
    assert err(doc).kind == kind


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nan_and_infinity_literals_are_non_finite(literal: str) -> None:
    # the first 263.2175 is regularMarketPrice; the last is the bar close
    body = FIXTURE.read_text()
    head, _, tail = body.rpartition("263.2175")
    assert (head + literal + tail) != body
    p = provider(Transport(HttpResponse(200, (head + literal + tail).encode())))
    with pytest.raises(PriceError) as e:
        p.get_price("AAPL", date(2026, 10, 16), "close")
    assert e.value.kind == "non_finite"


def test_price_rounds_half_up_to_4dp_without_float() -> None:
    body = FIXTURE.read_text().replace("262.0\n", "262.00005\n")
    assert "262.00005" in body
    p = provider(Transport(HttpResponse(200, body.encode())))
    assert p.get_price("AAPL", date(2026, 10, 16), "open").price == Decimal("262.0001")


def test_non_json_body_is_malformed() -> None:
    with pytest.raises(PriceError) as e:
        provider(Transport(HttpResponse(200, b"<html>Too Many Requests</html>"))).get_price(
            "AAPL", date(2026, 10, 16), "close"
        )
    assert e.value.kind == "malformed"


def test_cross_check_warns_on_large_disagreement(caplog: pytest.LogCaptureFixture) -> None:
    doc = mutate(lambda d: result(d)["meta"].update(regularMarketPrice=300.0))
    with caplog.at_level(logging.WARNING, logger="stockmanager.adapters.yahoo"):
        assert fetch(doc).price == Decimal("263.2175")
    assert "regularMarketPrice" in caplog.text


def test_cross_check_quiet_when_close(caplog: pytest.LogCaptureFixture) -> None:
    doc = mutate(lambda d: result(d)["meta"].update(regularMarketPrice=265.0))
    with caplog.at_level(logging.WARNING, logger="stockmanager.adapters.yahoo"):
        fetch(doc)
    assert caplog.text == ""


def test_cross_check_skipped_for_older_bars(caplog: pytest.LogCaptureFixture) -> None:
    doc = mutate(lambda d: result(d)["meta"].update(regularMarketPrice=500.0))
    with caplog.at_level(logging.WARNING, logger="stockmanager.adapters.yahoo"):
        fetch(doc, d=date(2026, 10, 13))
    assert caplog.text == ""


def test_retries_5xx_and_429_then_succeeds() -> None:
    sleeps: list[float] = []
    t = Transport(HttpResponse(503, b""), HttpResponse(429, b"", {"retry-after": "2"}), ok(load()))
    p = provider(t, sleeps=sleeps)
    assert p.get_price("AAPL", date(2026, 10, 16), "close").price == Decimal("263.2175")
    assert len(t.requests) == 3 and sleeps == [0.5, 2.0]


def test_gives_up_after_two_retries() -> None:
    t = Transport(*[HttpResponse(500, b"")] * 3)
    with pytest.raises(PriceError) as e:
        provider(t).get_price("AAPL", date(2026, 10, 16), "close")
    assert e.value.kind == "http_error" and len(t.requests) == 3


def test_client_errors_are_not_retried() -> None:
    t = Transport(HttpResponse(404, b"{}"))
    with pytest.raises(PriceError) as e:
        provider(t).get_price("ZZZZ", date(2026, 10, 16), "close")
    assert e.value.kind == "http_error" and len(t.requests) == 1


def test_retry_after_is_capped() -> None:
    sleeps: list[float] = []
    t = Transport(HttpResponse(429, b"", {"retry-after": "3600"}), ok(load()))
    provider(t, sleeps=sleeps).get_price("AAPL", date(2026, 10, 16), "close")
    assert sleeps == [30.0]


@pytest.mark.parametrize("exc", [TimeoutError("read timed out"), ConnectionResetError()])
def test_transport_failures_are_timeouts(exc: Exception) -> None:
    with pytest.raises(PriceError) as e:
        provider(Transport(exc)).get_price("AAPL", date(2026, 10, 16), "close")
    assert e.value.kind == "timeout"


def test_fixture_is_not_mutated_by_tests() -> None:
    assert copy.deepcopy(load()) == load()
