"""Yfinance-based price provider: cache, batch chunks, retry, stale fallback."""

from __future__ import annotations

import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from stockmanager.adapters.yfinance_provider import (
    CHUNK_SIZE,
    DEFAULT_TTL,
    YfinancePriceProvider,
    regular_close,
)
from stockmanager.pricing.errors import PriceError

# ── fixtures ──────────────────────────────────────────────────────────────────

PT = "US/Pacific"  # not used directly; tests inject a deterministic clock


def _now(year: int = 2026, month: int = 10, day: int = 16, hour: int = 14, minute: int = 0) -> datetime:
    """After 16:00 ET (19:00 PT) so close is settled."""
    return datetime(year, month, day, hour, minute, tzinfo=__import__("zoneinfo").ZoneInfo("America/Los_Angeles"))


def _provider(
    tmp_path: Path,
    now: datetime | None = None,
    sleeps: list[float] | None = None,
    cache_ttl: float = DEFAULT_TTL,
    chunk_size: int = CHUNK_SIZE,
) -> YfinancePriceProvider:
    now_fn = lambda: now or _now()
    sleep_fn = (sleeps if sleeps is not None else []).append
    return YfinancePriceProvider(
        now=now_fn,
        session_close=regular_close,
        sleep=sleep_fn,
        cache_dir=tmp_path,
        cache_ttl=cache_ttl,
        chunk_size=chunk_size,
    )


def _batch_df(tickers: list[str], session_date: date, close: dict[str, float], open_: dict[str, float] | None = None) -> pd.DataFrame:
    """Build a multi-ticker DataFrame like yf.download() returns."""
    data = {}
    for t in tickers:
        data[(t, "Close")] = [close.get(t, 100.0)]
        if open_:
            data[(t, "Open")] = [open_.get(t, 99.0)]
        else:
            data[(t, "Open")] = [99.0]
    idx = pd.DatetimeIndex([datetime.combine(session_date, pd.Timestamp("2000-01-01").time())])
    df = pd.DataFrame(data, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


def _single_df(ticker: str, session_date: date, close: float = 100.0, open_: float = 99.0) -> pd.DataFrame:
    """Single-ticker DataFrame like Ticker(...).history() returns."""
    idx = pd.DatetimeIndex([datetime.combine(session_date, pd.Timestamp("2000-01-01").time())])
    return pd.DataFrame({"Close": [close], "Open": [open_]}, index=idx)


# ── cache I/O ────────────────────────────────────────────────────────────────

def test_load_empty_cache(tmp_path: Path) -> None:
    p = _provider(tmp_path)
    assert p._cache == {}


def test_save_and_reload_cache(tmp_path: Path) -> None:
    p = _provider(tmp_path)
    # Manually insert a cache entry
    from stockmanager.adapters.yfinance_provider import _CacheEntry
    entry = _CacheEntry(
        ticker="AAPL", session_date="2026-10-16", kind="close",
        price="150.00", source="yahoo",
        fetched_at=datetime.now().isoformat(), cached_at=time.time(),
    )
    p._cache["AAPL|2026-10-16|close"] = entry
    p._save_cache()
    # Reload
    p2 = _provider(tmp_path)
    assert "AAPL|2026-10-16|close" in p2._cache
    assert p2._cache["AAPL|2026-10-16|close"].price == "150.00"


def test_cache_file_not_found_starts_empty(tmp_path: Path) -> None:
    p = _provider(tmp_path)
    assert p._cache == {}


def test_malformed_cache_entry_dropped(tmp_path: Path) -> None:
    cache_file = tmp_path / "yahoo_prices.json"
    cache_file.write_text('{"BAD|2026-10-16|close": {"ticker": 123}}')  # malformed
    p = _provider(tmp_path)
    assert p._cache == {}


# ── get_price ────────────────────────────────────────────────────────────────

def test_get_price_from_fresh_cache(tmp_path: Path) -> None:
    from stockmanager.adapters.yfinance_provider import _CacheEntry
    now = _now()
    p = _provider(tmp_path)
    entry = _CacheEntry(
        ticker="AAPL", session_date="2026-10-16", kind="close",
        price="150.00", source="yahoo",
        fetched_at=now.isoformat(), cached_at=now.timestamp(),
    )
    p._cache["AAPL|2026-10-16|close"] = entry
    quote = p.get_price("AAPL", date(2026, 10, 16), "close")
    assert quote.price == Decimal("150.0000")
    assert quote.ticker == "AAPL"


def test_get_price_fresh_cache_returns_without_fetch(tmp_path: Path) -> None:
    from stockmanager.adapters.yfinance_provider import _CacheEntry
    now = _now()
    p = _provider(tmp_path)
    entry = _CacheEntry(
        ticker="MSFT", session_date="2026-10-16", kind="close",
        price="300.00", source="yahoo",
        fetched_at=now.isoformat(), cached_at=now.timestamp(),
    )
    p._cache["MSFT|2026-10-16|close"] = entry
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        p.get_price("MSFT", date(2026, 10, 16), "close")
        mock_yf.Ticker.assert_not_called()


def test_get_price_stale_fetches_from_yfinance(tmp_path: Path) -> None:
    now = _now()
    p = _provider(tmp_path, now=now)
    session_date = date(2026, 10, 16)
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _single_df("AAPL", session_date, close=150.50)
        mock_ticker.info = {"currency": "USD", "quoteType": "EQUITY", "regularMarketPrice": 150.50}
        mock_yf.Ticker.return_value = mock_ticker
        quote = p.get_price("AAPL", session_date, "close")
        assert quote.price == Decimal("150.5000")
        assert quote.source == "yahoo"


def test_get_price_open_kind(tmp_path: Path) -> None:
    now = _now()
    p = _provider(tmp_path, now=now)
    session_date = date(2026, 10, 16)
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _single_df("AAPL", session_date, close=150.0, open_=148.5)
        mock_ticker.info = {"currency": "USD", "quoteType": "EQUITY"}
        mock_yf.Ticker.return_value = mock_ticker
        quote = p.get_price("AAPL", session_date, "open")
        assert quote.price == Decimal("148.5000")
        assert quote.kind == "open"


def test_get_price_close_before_settle(tmp_path: Path) -> None:
    """Close before 16:00 ET + 20min settle raises incomplete_session."""
    before_settle = datetime(2026, 10, 16, 12, 30, tzinfo=__import__("zoneinfo").ZoneInfo("America/Los_Angeles"))
    p = _provider(tmp_path, now=before_settle)
    with pytest.raises(PriceError, match="incomplete_session"):
        p.get_price("AAPL", date(2026, 10, 16), "close")


def test_get_price_open_before_settle_ok(tmp_path: Path) -> None:
    """Open does not require settle."""
    before_settle = datetime(2026, 10, 16, 6, 30, tzinfo=__import__("zoneinfo").ZoneInfo("America/Los_Angeles"))
    p = _provider(tmp_path, now=before_settle)
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _single_df("AAPL", date(2026, 10, 16))
        mock_ticker.info = {"currency": "USD", "quoteType": "EQUITY"}
        mock_yf.Ticker.return_value = mock_ticker
        quote = p.get_price("AAPL", date(2026, 10, 16), "open")
        assert quote.kind == "open"


def test_get_price_fallback_to_stale_cache_on_fetch_failure(tmp_path: Path) -> None:
    """When fetch fails, fall back to stale cache entry."""
    from stockmanager.adapters.yfinance_provider import _CacheEntry
    now = _now()
    p = _provider(tmp_path, now=now)
    # Insert a stale cache entry (TTL expired)
    old_ts = now.timestamp() - (DEFAULT_TTL + 60)
    entry = _CacheEntry(
        ticker="AAPL", session_date="2026-10-16", kind="close",
        price="150.00", source="yahoo",
        fetched_at=now.isoformat(), cached_at=old_ts,
    )
    p._cache["AAPL|2026-10-16|close"] = entry
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()  # empty = fetch failure
        mock_ticker.info = {}
        mock_yf.Ticker.return_value = mock_ticker
        quote = p.get_price("AAPL", date(2026, 10, 16), "close")
        assert quote.price == Decimal("150.0000")


def test_get_price_raises_when_no_cache_and_fetch_fails(tmp_path: Path) -> None:
    """When no cache entry exists and fetch fails, raises."""
    now = _now()
    p = _provider(tmp_path, now=now)
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        mock_ticker.info = {}
        mock_yf.Ticker.return_value = mock_ticker
        with pytest.raises(PriceError):
            p.get_price("AAPL", date(2026, 10, 16), "close")


# ── prime ────────────────────────────────────────────────────────────────────

def test_prime_batch_fetches_chunks(tmp_path: Path) -> None:
    """prime() fetches in chunks, sleeps between chunks."""
    now = _now()
    sleeps: list[float] = []
    session_date = date(2026, 10, 16)
    p = _provider(tmp_path, now=now, sleeps=sleeps, chunk_size=2)
    tickers = ["AAPL", "MSFT", "GOOGL", "AMZN"]
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        def mock_download(tickers: Any, *args: Any, **kwargs: Any) -> Any:
            return _batch_df(list(tickers), session_date, {t: 100.0 for t in tickers})
        mock_yf.download = mock_download
        failures = p.prime(tickers, session_date, "close")
        assert failures == {}
        # 2 chunks → 1 sleep between them
        assert len(sleeps) == 1
        assert 2.0 <= sleeps[0] <= 4.0


def test_prime_returns_failures_for_failed_symbols(tmp_path: Path) -> None:
    """Failed symbols returned in dict; successful ones cached."""
    now = _now()
    sleeps: list[float] = []
    session_date = date(2026, 10, 16)
    p = _provider(tmp_path, now=now, sleeps=sleeps, chunk_size=10)
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        def mock_download(tickers: Any, *args: Any, **kwargs: Any) -> Any:
            # Only AAPL succeeds — MSFT not in frame
            data = {("AAPL", "Close"): [150.0], ("AAPL", "Open"): [149.0]}
            idx = pd.DatetimeIndex([datetime.combine(session_date, pd.Timestamp("2000-01-01").time())])
            return pd.DataFrame(data, index=idx)
        mock_yf.download = mock_download
        # Single fetch for MSFT should also fail
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        mock_ticker.info = {}
        mock_yf.Ticker.return_value = mock_ticker
        failures = p.prime(["AAPL", "MSFT"], session_date, "close")
        assert "AAPL" not in failures
        assert "MSFT" in failures


def test_prime_no_stale_symbols_returns_empty(tmp_path: Path) -> None:
    """If all symbols are fresh in cache, no fetches happen."""
    from stockmanager.adapters.yfinance_provider import _CacheEntry
    now = _now()
    p = _provider(tmp_path, now=now)
    for t in ["AAPL", "MSFT"]:
        entry = _CacheEntry(
            ticker=t, session_date="2026-10-16", kind="close",
            price="100.00", source="yahoo",
            fetched_at=now.isoformat(), cached_at=now.timestamp(),
        )
        p._cache[f"{t}|2026-10-16|close"] = entry
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        failures = p.prime(["AAPL", "MSFT"], date(2026, 10, 16), "close")
        assert failures == {}
        mock_yf.download.assert_not_called()


def test_prime_close_before_settle(tmp_path: Path) -> None:
    """prime() for close before settle raises."""
    before_settle = datetime(2026, 10, 16, 12, 30, tzinfo=__import__("zoneinfo").ZoneInfo("America/Los_Angeles"))
    p = _provider(tmp_path, now=before_settle)
    with pytest.raises(PriceError, match="incomplete_session"):
        p.prime(["AAPL"], date(2026, 10, 16), "close")


def test_prime_deduplicates_tickers(tmp_path: Path) -> None:
    """Duplicate tickers are fetched only once."""
    now = _now()
    session_date = date(2026, 10, 16)
    p = _provider(tmp_path, now=now, chunk_size=10)
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        mock_download = MagicMock(side_effect=lambda tickers, *a, **k: _batch_df(list(tickers), session_date, {t: 100.0 for t in tickers}))
        mock_yf.download = mock_download
        p.prime(["AAPL", "AAPL", "MSFT"], session_date, "close")
        # Should have downloaded only AAPL + MSFT (de-duped)
        call_args = mock_download.call_args
        tickers = list(call_args[0][0])
        assert "AAPL" in tickers
        assert tickers.count("AAPL") == 1


# ── error handling ───────────────────────────────────────────────────────────

def test_empty_dataframe_is_missing_error(tmp_path: Path) -> None:
    now = _now()
    p = _provider(tmp_path, now=now)
    session_date = date(2026, 10, 16)
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        mock_ticker.info = {}
        mock_yf.Ticker.return_value = mock_ticker
        with pytest.raises(PriceError) as exc_info:
            p.get_price("AAPL", session_date, "close")
        assert exc_info.value.kind == "missing"


def test_null_price_is_missing(tmp_path: Path) -> None:
    now = _now()
    p = _provider(tmp_path, now=now)
    session_date = date(2026, 10, 16)
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        df = _single_df("AAPL", session_date, close=float("nan"))
        df["Close"] = [None]
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = df
        mock_ticker.info = {}
        mock_yf.Ticker.return_value = mock_ticker
        with pytest.raises(PriceError) as exc_info:
            p.get_price("AAPL", session_date, "close")
        assert exc_info.value.kind == "missing"


def test_date_after_last_bar_is_stale_date(tmp_path: Path) -> None:
    """If session_date is after the latest bar date, raises stale_date."""
    now = _now(2026, 10, 19)  # Monday
    p = _provider(tmp_path, now=now)
    # Data only has bar for Oct 16 (Friday)
    hist = _single_df("AAPL", date(2026, 10, 16))
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = hist
        mock_ticker.info = {"currency": "USD", "quoteType": "EQUITY"}
        mock_yf.Ticker.return_value = mock_ticker
        with pytest.raises(PriceError) as exc_info:
            p.get_price("AAPL", date(2026, 10, 19), "close")
        assert exc_info.value.kind == "stale_date"


def test_non_usd_currency_raises(tmp_path: Path) -> None:
    now = _now()
    p = _provider(tmp_path, now=now)
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _single_df("AAPL", date(2026, 10, 16))
        mock_ticker.info = {"currency": "EUR", "quoteType": "EQUITY"}
        mock_yf.Ticker.return_value = mock_ticker
        with pytest.raises(PriceError) as exc_info:
            p.get_price("AAPL", date(2026, 10, 16), "close")
        assert "EUR" in exc_info.value.detail


def test_non_equity_instrument_raises(tmp_path: Path) -> None:
    now = _now()
    p = _provider(tmp_path, now=now)
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _single_df("AAPL", date(2026, 10, 16))
        mock_ticker.info = {"currency": "USD", "quoteType": "CRYPTOCURRENCY"}
        mock_yf.Ticker.return_value = mock_ticker
        with pytest.raises(PriceError):
            p.get_price("AAPL", date(2026, 10, 16), "close")


# ── price rounding ───────────────────────────────────────────────────────────

def test_price_rounds_to_4dp(tmp_path: Path) -> None:
    now = _now()
    p = _provider(tmp_path, now=now)
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _single_df("AAPL", date(2026, 10, 16), close=150.12345)
        mock_ticker.info = {"currency": "USD", "quoteType": "EQUITY", "regularMarketPrice": 150.12345}
        mock_yf.Ticker.return_value = mock_ticker
        quote = p.get_price("AAPL", date(2026, 10, 16), "close")
        assert quote.price == Decimal("150.1235")


# ── close flush ──────────────────────────────────────────────────────────────

def test_close_flushes_cache(tmp_path: Path) -> None:
    from stockmanager.adapters.yfinance_provider import _CacheEntry
    now = _now()
    p = _provider(tmp_path, now=now)
    entry = _CacheEntry(
        ticker="AAPL", session_date="2026-10-16", kind="close",
        price="150.00", source="yahoo",
        fetched_at=now.isoformat(), cached_at=now.timestamp(),
    )
    p._cache["AAPL|2026-10-16|close"] = entry
    p.close()
    assert (tmp_path / "yahoo_prices.json").exists()


# ── retry with exponential backoff ──────────────────────────────────────────

def test_prime_retries_failed_symbols(tmp_path: Path) -> None:
    """Failed symbols are retried with exponential backoff."""
    now = _now()
    sleeps: list[float] = []
    session_date = date(2026, 10, 16)
    p = _provider(tmp_path, now=now, sleeps=sleeps, chunk_size=10)
    call_count = 0

    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        def mock_download(tickers: Any, *args: Any, **kwargs: Any) -> Any:
            # Only AAPL fails in batch; MSFT succeeds
            data = {
                ("AAPL", "Close"): [None],
                ("AAPL", "Open"): [99.0],
                ("MSFT", "Close"): [300.0],
                ("MSFT", "Open"): [299.0],
            }
            idx = pd.DatetimeIndex([datetime.combine(session_date, pd.Timestamp("2000-01-01").time())])
            return pd.DataFrame(data, index=idx)
        mock_yf.download = mock_download

        def mock_history(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return pd.DataFrame()
            return _single_df("AAPL", session_date, close=150.0)

        mock_ticker = MagicMock()
        mock_ticker.history = mock_history
        mock_ticker.info = {"currency": "USD", "quoteType": "EQUITY", "regularMarketPrice": 150.0}
        mock_yf.Ticker.return_value = mock_ticker

        failures = p.prime(["AAPL", "MSFT"], session_date, "close")
        # AAPL fails first time, succeeds second — 1 sleep, not in failures
        assert "AAPL" not in failures
        assert "MSFT" not in failures
        assert call_count == 2
        assert len(sleeps) >= 1  # at least 1 retry sleep for AAPL


# ── regular_close helper ────────────────────────────────────────────────────

def test_regular_close_returns_1600_et() -> None:
    dt = regular_close(date(2026, 10, 16))
    assert dt.hour == 16 and dt.minute == 0
    assert dt.tzinfo is not None


# ── cross-check warning ─────────────────────────────────────────────────────

def test_cross_check_warns_on_large_disagreement(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    now = _now()
    p = _provider(tmp_path, now=now)
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _single_df("AAPL", date(2026, 10, 16), close=150.0)
        # regularMarketPrice differs by > 5%
        mock_ticker.info = {"currency": "USD", "quoteType": "EQUITY", "regularMarketPrice": 200.0}
        mock_yf.Ticker.return_value = mock_ticker
        import logging
        with caplog.at_level(logging.WARNING, logger="stockmanager.adapters.yfinance_provider"):
            p.get_price("AAPL", date(2026, 10, 16), "close")
        assert "regularMarketPrice" in caplog.text


# ── ticker case normalization ───────────────────────────────────────────────

def test_get_price_uppercases_ticker(tmp_path: Path) -> None:
    now = _now()
    p = _provider(tmp_path, now=now)
    with patch("stockmanager.adapters.yfinance_provider.yf") as mock_yf:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _single_df("aapl", date(2026, 10, 16))
        mock_ticker.info = {"currency": "USD", "quoteType": "EQUITY"}
        mock_yf.Ticker.return_value = mock_ticker
        quote = p.get_price("aapl", date(2026, 10, 16), "close")
        assert quote.ticker == "AAPL"
