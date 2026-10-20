"""Token bucket enforcement and circuit breaker threshold (news-sources §2.2, §2.3)."""

import threading
import time

import pytest

from stockmanager.news.fetchers.base import NewsUnavailable
from stockmanager.news.ratelimit import CircuitBreaker, RateLimited, TokenBucket


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def bucket(t: FakeTime, rate: float = 25, burst: int = 5) -> TokenBucket:
    return TokenBucket(rate, burst, clock=t.clock, sleep=t.sleep)


def test_burst_is_free_then_paced() -> None:
    t = FakeTime()
    b = bucket(t)
    for _ in range(5):
        assert b.acquire() == 0.0
    assert t.slept == []
    assert b.acquire() == pytest.approx(2.4)  # 25/min = one token per 2.4 s


def test_never_exceeds_rate_under_a_40_holding_plan() -> None:
    t = FakeTime()
    b = bucket(t)
    start = t.now
    stamps = []
    for _ in range(120):  # 40 holdings x 3 calls
        b.acquire()
        stamps.append(t.now - start)
    for n, at in enumerate(stamps):
        assert at >= max(0.0, (n + 1 - 5) * 2.4) - 1e-9  # burst of 5, then 25 per minute
    for i in range(len(stamps)):  # no 60 s window holds more than 25 + burst calls
        assert sum(1 for s in stamps if stamps[i] <= s < stamps[i] + 60) <= 25 + 5


def test_idle_time_refills_up_to_burst_only() -> None:
    t = FakeTime()
    b = bucket(t)
    for _ in range(5):
        b.acquire()
    t.now += 3600
    for _ in range(5):
        assert b.acquire() == 0.0
    assert b.acquire() > 0


def test_max_wait_refuses_without_consuming_a_token() -> None:
    t = FakeTime()
    b = bucket(t, burst=1)
    b.acquire()
    with pytest.raises(RateLimited) as e:
        b.acquire(max_wait=1.0)
    assert e.value.kind == "rate" and isinstance(e.value, NewsUnavailable) and e.value.wait == pytest.approx(2.4)
    assert t.slept == []
    assert b.acquire(max_wait=2.5) == pytest.approx(2.4)  # the refused call took nothing


def test_concurrent_callers_queue_behind_each_other() -> None:
    b = TokenBucket(6000, burst=1)  # 100 per second
    start = time.monotonic()
    threads = [threading.Thread(target=lambda: [b.acquire() for _ in range(5)]) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert time.monotonic() - start >= 0.3  # 40 calls, 1 free: ~0.39 s


def test_invalid_parameters() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0)
    with pytest.raises(ValueError):
        TokenBucket(10, burst=0)


def test_breaker_opens_at_three_consecutive_failures() -> None:
    cb = CircuitBreaker(3)
    cb.record_failure()
    cb.record_failure()
    assert not cb.is_open
    cb.record_failure()
    assert cb.is_open


def test_success_resets_the_consecutive_count() -> None:
    cb = CircuitBreaker(3)
    for _ in range(2):
        cb.record_failure()
    cb.record_success()
    for _ in range(2):
        cb.record_failure()
    assert not cb.is_open


def test_trip_opens_at_once_and_stays_open() -> None:
    cb = CircuitBreaker(3)
    cb.trip()
    assert cb.is_open
    cb.record_success()
    assert cb.is_open  # open for the rest of the cycle
