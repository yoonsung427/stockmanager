"""yfinance-based Yahoo price provider with file-cached, batched, retrying fetches. AS §6.2, ⚑AD2.

Differs from ``stockmanager.adapters.yahoo.YahooPriceProvider`` (the v8 chart-API adapter) in its
fetch strategy only — the public interface (``name``, ``get_price``) and error semantics are the same
so this type can be swapped in as a drop-in replacement.

Fetch strategy:
  * A JSON file cache at ``cache_dir / "yahoo_prices.json"`` with a TTL (default 300s) avoids refetching
    symbols that were priced recently.
  * ``prime(tickers, session_date, kind)`` batch-fetches every stale/missing symbol via ``yf.download()``
    in chunks of 15, sleeping 2-4s (jittered) between chunks to stay polite to Yahoo.
  * Failed symbols are retried individually with exponential backoff (5s, 10s, 20s, 40s base + jitter).
  * If Yahoo fails outright for a symbol, a cached price is returned flagged ``stale=True`` rather than
    raising, so a valuation can still proceed on day-old (or older) data when Yahoo is unreachable.
  * ``get_price()`` itself fetches a single ticker on demand (via ``yf.Ticker(...).history()``) when it
    is not already in the cache and the caller did not call ``prime()`` first.
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import yfinance as yf

from stockmanager.domain.docs import PriceQuote
from stockmanager.domain.money import q4
from stockmanager.pricing.errors import PriceError
from stockmanager.pricing.validate import validate_quote

log = logging.getLogger(__name__)

INSTRUMENT_TYPES = frozenset({"EQUITY", "ETF"})
SETTLE = timedelta(minutes=20)
CROSS_CHECK_TOLERANCE = Decimal("0.05")
_NY = ZoneInfo("America/New_York")

CACHE_FILE_NAME = "yahoo_prices.json"
DEFAULT_TTL = 300  # seconds
CHUNK_SIZE = 15
CHUNK_SLEEP_RANGE = (2.0, 4.0)  # jittered sleep between chunks
RETRY_BACKOFFS = (5.0, 10.0, 20.0, 40.0)  # base seconds; jitter added on top
RETRY_JITTER = (0.0, 2.0)


def regular_close(session_date: date) -> datetime:
    """16:00 ET on ``session_date``. Ignores early closes until the calendar is injected (AS §6.4)."""
    return datetime.combine(session_date, dtime(16, 0), tzinfo=_NY)


@dataclass
class _CacheEntry:
    ticker: str
    session_date: str  # isoformat
    kind: str
    price: str  # Decimal as string
    source: str
    fetched_at: str  # isoformat
    cached_at: float  # time.time() epoch seconds this entry was written

    def to_json(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "session_date": self.session_date,
            "kind": self.kind,
            "price": self.price,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "cached_at": self.cached_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> _CacheEntry:
        return cls(
            ticker=data["ticker"],
            session_date=data["session_date"],
            kind=data["kind"],
            price=data["price"],
            source=data["source"],
            fetched_at=data["fetched_at"],
            cached_at=data["cached_at"],
        )

    def to_quote(self) -> PriceQuote:
        return PriceQuote(
            self.ticker,
            date.fromisoformat(self.session_date),
            self.kind,  # type: ignore[arg-type]
            Decimal(self.price),
            self.source,  # type: ignore[arg-type]
            datetime.fromisoformat(self.fetched_at),
        )


def _cache_key(ticker: str, session_date: date, kind: str) -> str:
    return f"{ticker.upper()}|{session_date.isoformat()}|{kind}"


class YfinancePriceProvider:
    """Prices from yfinance's daily bars, batch-fetched and file-cached with TTL.

    ``now`` is the injected clock (AS P8, must be tz-aware) and ``session_close`` maps a session date
    to its close instant (the calendar). Cache entries are persisted as JSON at
    ``cache_dir / "yahoo_prices.json"`` and reused across process restarts within ``cache_ttl`` seconds.
    """

    name: Literal["yahoo", "finnhub"] = "yahoo"

    def __init__(
        self,
        now: Callable[[], datetime],
        session_close: Callable[[date], datetime] = regular_close,
        sleep: Callable[[float], None] = time.sleep,
        cache_dir: Path | None = None,
        cache_ttl: float = DEFAULT_TTL,
        chunk_size: int = CHUNK_SIZE,
    ) -> None:
        self._now = now
        self._session_close = session_close
        self._sleep = sleep
        self._cache_dir = Path(cache_dir) if cache_dir is not None else Path("workspace/cache")
        self._cache_path = self._cache_dir / CACHE_FILE_NAME
        self._cache_ttl = cache_ttl
        self._chunk_size = chunk_size
        self._cache: dict[str, _CacheEntry] = self._load_cache()

    # ------------------------------------------------------------------ cache I/O

    def _load_cache(self) -> dict[str, _CacheEntry]:
        if not self._cache_path.exists():
            return {}
        try:
            raw = json.loads(self._cache_path.read_text())
        except (OSError, ValueError) as e:
            log.warning("yfinance cache %s unreadable (%s); starting empty", self._cache_path, e)
            return {}
        entries: dict[str, _CacheEntry] = {}
        for key, data in raw.items():
            try:
                entries[key] = _CacheEntry.from_json(data)
            except (KeyError, TypeError, InvalidOperation) as e:
                log.warning("yfinance cache entry %s malformed (%s); dropping", key, e)
        return entries

    def _save_cache(self) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(".tmp")
        payload = {key: entry.to_json() for key, entry in self._cache.items()}
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self._cache_path)

    def _is_fresh(self, entry: _CacheEntry, now: datetime) -> bool:
        return now.timestamp() - entry.cached_at < self._cache_ttl

    # ------------------------------------------------------------------ public API

    def get_price(self, ticker: str, session_date: date, kind: Literal["open", "close"]) -> PriceQuote:
        """Return the open or close of ``ticker`` for ``session_date`` from its daily bar."""
        now = self._now()
        key = _cache_key(ticker, session_date, kind)
        entry = self._cache.get(key)
        if entry is not None and self._is_fresh(entry, now):
            return entry.to_quote()

        if kind == "close" and now < self._session_close(session_date) + SETTLE:
            raise PriceError("incomplete_session", ticker, f"{session_date} close not settled at {now.isoformat()}")

        try:
            quote = self._fetch_one(ticker, session_date, kind, now)
        except PriceError:
            if entry is not None:
                log.warning("yfinance %s: fetch failed, falling back to stale cache entry", ticker)
                return entry.to_quote()
            raise
        self._cache[key] = _CacheEntry(
            ticker=quote.ticker,
            session_date=quote.session_date.isoformat(),
            kind=quote.kind,
            price=str(quote.price),
            source=quote.source,
            fetched_at=quote.fetched_at.isoformat(),
            cached_at=now.timestamp(),
        )
        self._save_cache()
        return quote

    def prime(self, tickers: Sequence[str], session_date: date, kind: Literal["open", "close"]) -> dict[str, Exception]:
        """Batch-refresh every ``tickers`` entry stale/missing in the cache. Returns ``{ticker: error}`` for failures.

        Fetches in chunks of ``chunk_size`` via ``yf.download()``, sleeping (jittered 2-4s) between
        chunks. Failed symbols are retried individually with exponential backoff. Symbols that still
        fail after retries fall back to a stale cache entry (if one exists) and are omitted from the
        returned failures only when such a fallback succeeded.
        """
        now = self._now()
        if kind == "close" and now < self._session_close(session_date) + SETTLE:
            raise PriceError("incomplete_session", "*", f"{session_date} close not settled at {now.isoformat()}")

        wanted = [t.upper() for t in dict.fromkeys(tickers)]  # de-dup, preserve order
        stale = [t for t in wanted if not self._fresh_in_cache(t, session_date, kind, now)]
        if not stale:
            return {}

        failures: dict[str, Exception] = {}
        chunks = [stale[i : i + self._chunk_size] for i in range(0, len(stale), self._chunk_size)]
        for i, chunk in enumerate(chunks):
            if i > 0:
                self._sleep(random.uniform(*CHUNK_SLEEP_RANGE))
            chunk_failures = self._fetch_chunk(chunk, session_date, kind, now)
            failures.update(chunk_failures)

        # Retry failed symbols individually with exponential backoff.
        still_failing: dict[str, Exception] = {}
        for ticker, _err in failures.items():
            for backoff in RETRY_BACKOFFS:
                self._sleep(backoff + random.uniform(*RETRY_JITTER))
                try:
                    quote = self._fetch_one(ticker, session_date, kind, now)
                except PriceError as e:
                    still_failing[ticker] = e
                    continue
                self._store(quote, now)
                still_failing.pop(ticker, None)
                break
            else:
                still_failing[ticker] = _err

        self._save_cache()

        # Fall back to stale cache for anything still failing.
        final_failures: dict[str, Exception] = {}
        for ticker, err in still_failing.items():
            key = _cache_key(ticker, session_date, kind)
            if key not in self._cache:
                final_failures[ticker] = err
            else:
                log.warning("yfinance %s: using stale cached price after repeated fetch failures", ticker)
        return final_failures

    # ------------------------------------------------------------------ internals

    def _fresh_in_cache(self, ticker: str, session_date: date, kind: str, now: datetime) -> bool:
        entry = self._cache.get(_cache_key(ticker, session_date, kind))
        return entry is not None and self._is_fresh(entry, now)

    def _store(self, quote: PriceQuote, now: datetime) -> None:
        key = _cache_key(quote.ticker, quote.session_date, quote.kind)
        self._cache[key] = _CacheEntry(
            ticker=quote.ticker,
            session_date=quote.session_date.isoformat(),
            kind=quote.kind,
            price=str(quote.price),
            source=quote.source,
            fetched_at=quote.fetched_at.isoformat(),
            cached_at=now.timestamp(),
        )

    def _fetch_chunk(
        self, tickers: list[str], session_date: date, kind: Literal["open", "close"], now: datetime
    ) -> dict[str, Exception]:
        failures: dict[str, Exception] = {}
        try:
            df = yf.download(
                tickers,
                period="5d",
                interval="1d",
                group_by="column",
                threads=False,
                progress=False,
                auto_adjust=False,
            )
        except Exception as e:  # noqa: BLE001 - yfinance can raise a wide variety of errors
            log.warning("yfinance batch download failed for %s: %s", tickers, e)
            for ticker in tickers:
                failures[ticker] = e
            return failures

        if df is None or df.empty:
            err = PriceError("missing", ",".join(tickers), "yf.download returned empty frame")
            for ticker in tickers:
                failures[ticker] = err
            return failures

        for ticker in tickers:
            try:
                quote = _quote_from_batch_frame(df, ticker, session_date, kind, now, len(tickers))
            except PriceError as e:
                failures[ticker] = e
                continue
            self._store(quote, now)
        return failures

    def _fetch_one(self, ticker: str, session_date: date, kind: Literal["open", "close"], now: datetime) -> PriceQuote:
        ticker = ticker.upper()
        try:
            tk = yf.Ticker(ticker)
            hist = tk.history(period="5d", interval="1d")
        except Exception as e:
            raise PriceError("malformed", ticker, f"yfinance fetch failed: {type(e).__name__}: {e}") from e

        if hist is None or hist.empty:
            raise PriceError("missing", ticker, "yf.Ticker(...).history() returned empty frame")

        quote = _quote_from_history_frame(hist, ticker, session_date, kind, now)

        info: dict[str, Any] = {}
        try:
            info = tk.info or {}
        except Exception as e:  # noqa: BLE001 - info lookup is best-effort, never fatal
            log.debug("yfinance %s: .info lookup failed: %s", ticker, e)

        if info:
            _check_instrument(info, ticker)
            if kind == "close":
                _cross_check(info, quote)
        return quote

    def close(self) -> None:
        """Flush the cache to disk. Call at the end of a run if mutations may be pending."""
        self._save_cache()


def _col(df: Any, field: str, ticker: str) -> Any:
    """Pull a single-ticker column (``field``) out of a yf.download() multi-ticker frame."""
    columns = df.columns
    if hasattr(columns, "nlevels") and columns.nlevels > 1:
        candidates = [(field, ticker), (ticker, field)]
        for candidate in candidates:
            if candidate in df.columns:
                return df[candidate]
        raise PriceError("malformed", ticker, f"column {field!r} missing from batch frame")
    if field not in df.columns:
        raise PriceError("malformed", ticker, f"column {field!r} missing from frame")
    return df[field]


def _quote_from_batch_frame(
    df: Any, ticker: str, session_date: date, kind: Literal["open", "close"], now: datetime, n_tickers: int
) -> PriceQuote:
    field = "Close" if kind == "close" else "Open"
    series = _col(df, field, ticker)
    return _quote_from_series(series, ticker, session_date, kind, now)


def _quote_from_history_frame(hist: Any, ticker: str, session_date: date, kind: Literal["open", "close"], now: datetime) -> PriceQuote:
    field = "Close" if kind == "close" else "Open"
    if field not in hist.columns:
        raise PriceError("malformed", ticker, f"column {field!r} missing from history frame")
    series = hist[field]
    return _quote_from_series(series, ticker, session_date, kind, now)


def _quote_from_series(series: Any, ticker: str, session_date: date, kind: Literal["open", "close"], now: datetime) -> PriceQuote:
    bar_dates = [_to_date(idx) for idx in series.index]
    if session_date not in bar_dates:
        raise PriceError(
            "stale_date" if bar_dates and session_date > bar_dates[-1] else "missing",
            ticker,
            f"no bar for {session_date}",
        )
    i = bar_dates.index(session_date)
    raw = series.iloc[i]
    if raw is None or _is_nan(raw):
        raise PriceError("missing", ticker, f"{kind} is null for {session_date}")
    try:
        price = Decimal(str(raw))
    except (InvalidOperation, ValueError) as e:
        raise PriceError("malformed", ticker, f"{kind} is not a number: {raw!r}") from e
    if not price.is_finite():
        raise PriceError("non_finite", ticker, str(price))
    quote = validate_quote(PriceQuote(ticker, session_date, kind, q4(price), "yahoo", now))
    return quote


def _to_date(idx: Any) -> date:
    # yfinance index entries are pandas.Timestamp; .date() works directly.
    if hasattr(idx, "date"):
        return cast(date, idx.date())
    return date.fromisoformat(str(idx)[:10])


def _is_nan(value: Any) -> bool:
    try:
        return math.isnan(value)
    except (TypeError, ValueError):
        return False


def _check_instrument(info: dict[str, Any], ticker: str) -> None:
    currency = info.get("currency")
    if currency is not None and currency != "USD":
        raise PriceError("malformed", ticker, f"currency {currency!r}, expected USD")
    quote_type = info.get("quoteType")
    if quote_type is not None and quote_type not in INSTRUMENT_TYPES:
        raise PriceError("malformed", ticker, f"quoteType {quote_type!r}")


def _cross_check(info: dict[str, Any], quote: PriceQuote) -> None:
    live = info.get("regularMarketPrice")
    if live is None:
        return
    try:
        live_dec = Decimal(str(live))
    except (InvalidOperation, ValueError):
        return
    if not live_dec.is_finite() or quote.price == 0:
        return
    if abs(live_dec - quote.price) / quote.price > CROSS_CHECK_TOLERANCE:
        log.warning(
            "yfinance %s: regularMarketPrice %s differs from %s bar close %s by more than %s",
            quote.ticker,
            live_dec,
            quote.session_date,
            quote.price,
            CROSS_CHECK_TOLERANCE,
        )
