"""Per-adapter rate limiting and the per-cycle circuit breaker (news-sources §2.2, §2.3)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from stockmanager.news.fetchers.base import NewsUnavailable


class RateLimited(NewsUnavailable):
    """The wait for a token would exceed what the caller can afford; the call is skipped, not queued."""

    def __init__(self, wait: float) -> None:
        super().__init__(f"rate limit: would wait {wait:.1f}s", kind="rate")
        self.wait = wait


class TokenBucket:
    """Token bucket shared by every query of one adapter in a cycle.

    A caller that finds the bucket empty reserves the next token (the balance goes negative) and sleeps outside the
    lock, so concurrent callers queue behind each other and the long-run rate never exceeds ``rate_per_min``.
    """

    def __init__(
        self,
        rate_per_min: float,
        burst: int = 5,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_min <= 0 or burst < 1:
            raise ValueError("rate_per_min must be > 0 and burst >= 1")
        self._rate = rate_per_min / 60.0
        self._burst = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens = float(burst)
        self._last = clock()

    def acquire(self, max_wait: float | None = None) -> float:
        """Take one token, sleeping if needed; return the time slept.

        Raises ``RateLimited`` without taking a token if the wait would exceed ``max_wait``.
        """
        with self._lock:
            now = self._clock()
            self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
            self._last = now
            wait = 0.0 if self._tokens >= 1.0 else (1.0 - self._tokens) / self._rate
            if max_wait is not None and wait > max_wait:
                raise RateLimited(wait)
            self._tokens -= 1.0
        if wait > 0:
            self._sleep(wait)
        return wait


class CircuitBreaker:
    """Opens after ``failures_to_open`` consecutive hard failures, or at once on an auth failure.

    An open breaker lasts for the rest of the cycle; a new cycle builds a new breaker.
    """

    def __init__(self, failures_to_open: int = 3) -> None:
        self._limit = failures_to_open
        self._lock = threading.Lock()
        self._consecutive = 0
        self._open = False

    @property
    def is_open(self) -> bool:
        """True once the adapter should be skipped for the rest of the cycle."""
        with self._lock:
            return self._open

    def record_success(self) -> None:
        """A successful call (an empty result counts) resets the consecutive count."""
        with self._lock:
            self._consecutive = 0

    def record_failure(self) -> None:
        """A hard failure; opens the breaker at the limit."""
        with self._lock:
            self._consecutive += 1
            if self._consecutive >= self._limit:
                self._open = True

    def trip(self) -> None:
        """Open immediately (401/403)."""
        with self._lock:
            self._open = True
