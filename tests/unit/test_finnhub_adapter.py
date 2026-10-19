"""Finnhub adapter: header auth, latest-session-only date check, validation."""

import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pytest

from stockmanager.adapters.finnhub import FinnhubPriceProvider
from stockmanager.adapters.http import HttpResponse
from stockmanager.pricing.errors import PriceError

PT = ZoneInfo("America/Los_Angeles")
NY = ZoneInfo("America/New_York")
D = date(2026, 10, 16)
NOW = datetime(2026, 10, 17, 9, 0, tzinfo=PT)  # Saturday
Kind = Literal["open", "close"]


def quote_doc(**kw: Any) -> dict[str, Any]:
    traded = int(datetime(2026, 10, 16, 16, 0, 1, tzinfo=NY).timestamp())
    return {"c": 263.2175, "o": 262.0, "h": 264.0, "l": 261.0, "pc": 261.75, "t": traded, **kw}


def run(doc: dict[str, Any], d: date = D, kind: Kind = "close", now: datetime = NOW, status: int = 200) -> Any:
    seen: list[tuple[str, Mapping[str, str]]] = []

    def get(url: str, headers: Mapping[str, str]) -> HttpResponse:
        seen.append((url, headers))
        return HttpResponse(status, json.dumps(doc).encode())

    p = FinnhubPriceProvider("sekret", now=lambda: now, http_get=get, sleep=lambda _: None)
    q = p.get_price("aapl", d, kind)
    assert seen[0][0] == "https://finnhub.io/api/v1/quote?symbol=AAPL"
    assert seen[0][1]["X-Finnhub-Token"] == "sekret" and "sekret" not in seen[0][0]
    return q


def kind_of(doc: dict[str, Any], **kw: Any) -> str:
    with pytest.raises(PriceError) as e:
        run(doc, **kw)
    return e.value.kind


def test_close_and_open() -> None:
    q = run(quote_doc())
    assert (q.price, q.source, q.kind) == (Decimal("263.2175"), "finnhub", "close")
    assert run(quote_doc(), kind="open", now=datetime(2026, 10, 16, 7, 0, tzinfo=PT)).price == Decimal("262.0000")


def test_older_session_is_stale() -> None:
    assert kind_of(quote_doc(), d=date(2026, 10, 15)) == "stale_date"


def test_close_before_settle_is_incomplete() -> None:
    assert kind_of(quote_doc(), now=datetime(2026, 10, 16, 13, 10, tzinfo=PT)) == "incomplete_session"


def test_after_hours_print_is_not_the_close() -> None:
    late = int(datetime(2026, 10, 16, 18, 0, tzinfo=NY).timestamp())
    assert kind_of(quote_doc(t=late)) == "incomplete_session"


@pytest.mark.parametrize("doc", [{"c": 0, "o": 0, "pc": 0, "t": 0}, quote_doc(c=-1)])
def test_unknown_symbol_zeros_and_negatives(doc: dict[str, Any]) -> None:
    assert kind_of(doc) == "non_positive"


def test_malformed() -> None:
    assert kind_of({"error": "x"}) == "malformed"
    assert kind_of(quote_doc(c="263")) == "malformed"
    assert kind_of(quote_doc(t=None)) == "malformed"


def test_http_error() -> None:
    assert kind_of({}, status=401) == "http_error"
