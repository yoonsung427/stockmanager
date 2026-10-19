"""portfolio.lock: acquire/release, stale break, PID reuse (AS §3.7, §5.4)."""

from __future__ import annotations

import json
import os
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from stockmanager.store.lock import LockBusy, LockMeta, PortfolioLock, ProcPidChecker
from tests.support.books import START, FakePidChecker, FixedClock

DEAD_PID = 999_999


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def pids() -> FakePidChecker:
    return FakePidChecker()


@pytest.fixture
def lock(tmp_path: Path, clock: FixedClock, pids: FakePidChecker) -> PortfolioLock:
    sleeps: list[float] = []
    lk = PortfolioLock(tmp_path / "ws", clock=clock, pid_checker=pids, sleep=sleeps.append)
    lk.sleeps = sleeps  # type: ignore[attr-defined]
    return lk


def plant(
    lock: PortfolioLock, clock: FixedClock, *, age_min: float, pid: int = DEAD_PID, token: str = "ab" * 16
) -> None:
    """Write a lock file as if another process had taken it ``age_min`` minutes ago."""
    meta = LockMeta(
        schema_version=1,
        token=token,
        pid=pid,
        proc_start=START,
        host="other",
        job="evaluation",
        acquired_at=clock.now - timedelta(minutes=age_min),
    )
    lock.root.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(json.dumps(meta.model_dump(mode="json")))


def test_acquire_writes_metadata_with_private_mode(lock: PortfolioLock, clock: FixedClock) -> None:
    held = lock.acquire("execution", 1, 0)
    assert lock.is_held()
    raw = json.loads(lock.path.read_text())
    assert set(raw) == {"schema_version", "token", "pid", "proc_start", "host", "job", "acquired_at"}
    assert raw["schema_version"] == 1 and raw["job"] == "execution" and raw["pid"] == os.getpid()
    assert raw["token"] == held.token and len(held.token) == 32 and int(held.token, 16) >= 0
    assert raw["acquired_at"] == clock.now.isoformat()
    assert stat.S_IMODE(lock.path.stat().st_mode) == 0o600


def test_release_unlinks_with_matching_token(lock: PortfolioLock) -> None:
    held = lock.acquire("a", 1, 0)
    assert lock.release(held.token) is True
    assert not lock.is_held()


def test_release_with_wrong_token_keeps_lock(lock: PortfolioLock) -> None:
    held = lock.acquire("a", 1, 0)
    assert lock.release("0" * 32) is False
    assert lock.is_held()
    assert lock.release(held.token) is True


def test_release_when_absent_is_false(lock: PortfolioLock) -> None:
    assert lock.release("x") is False


def test_second_acquire_is_busy_after_all_attempts_with_sleeps_between(lock: PortfolioLock) -> None:
    lock.acquire("a", 1, 0)
    with pytest.raises(LockBusy):
        lock.acquire("b", 3, 10.0)
    assert lock.sleeps == [10.0, 10.0]  # type: ignore[attr-defined]  # no sleep after the final attempt


def test_acquire_after_release(lock: PortfolioLock) -> None:
    lock.release(lock.acquire("a", 1, 0).token)
    assert lock.acquire("b", 1, 0).job == "b"


def test_symlink_at_lock_path_is_never_followed(lock: PortfolioLock, tmp_path: Path) -> None:
    lock.root.mkdir(parents=True)
    target = tmp_path / "victim"
    lock.path.symlink_to(target)
    assert lock.is_held()
    with pytest.raises(LockBusy):
        lock.acquire("a", 1, 0)
    assert not target.exists()


def test_fresh_lock_with_dead_owner_is_not_broken(lock: PortfolioLock, clock: FixedClock) -> None:
    plant(lock, clock, age_min=14)
    assert lock.break_stale(15) is None
    with pytest.raises(LockBusy):
        lock.acquire("b", 1, 0)


def test_old_lock_with_live_owner_is_never_broken(lock: PortfolioLock, clock: FixedClock, pids: FakePidChecker) -> None:
    pids.alive_pids[4242] = START
    plant(lock, clock, age_min=600, pid=4242)
    assert lock.break_stale(15) is None
    with pytest.raises(LockBusy):
        lock.acquire("b", 1, 0)
    assert lock.is_held()


def test_old_lock_with_dead_owner_is_broken_renamed_and_logged(lock: PortfolioLock, clock: FixedClock) -> None:
    plant(lock, clock, age_min=16, token="cd" * 16)
    assert lock.break_stale(15) == "cd" * 16
    assert not lock.is_held()
    assert (lock.root / f"portfolio.lock.broken.{'cd' * 16}").exists()
    (note,) = (lock.root / "run-notes").glob("*-lockbreak-*.md")
    text = note.read_text()
    assert "outcome: lock_broken" in text and "evaluation" in text and str(DEAD_PID) in text


def test_acquire_breaks_stale_lock_and_takes_it(lock: PortfolioLock, clock: FixedClock) -> None:
    plant(lock, clock, age_min=30)
    held = lock.acquire("b", 1, 0)
    assert json.loads(lock.path.read_text())["token"] == held.token
    assert len(list(lock.root.glob("portfolio.lock.broken.*"))) == 1


def test_pid_reuse_is_detected_as_dead(lock: PortfolioLock, clock: FixedClock, pids: FakePidChecker) -> None:
    """The recorded PID is running again, but it started after the lock's owner: a different process."""
    pids.alive_pids[4242] = START + timedelta(hours=3)
    plant(lock, clock, age_min=60, pid=4242)
    assert lock.break_stale(15) is not None


def test_unparsable_lock_is_held_never_stale(lock: PortfolioLock) -> None:
    lock.root.mkdir(parents=True)
    lock.path.write_text("{ not json")
    assert lock.break_stale(0) is None
    with pytest.raises(LockBusy):
        lock.acquire("b", 1, 0)
    assert lock.path.read_text() == "{ not json"


def test_unknown_fields_make_lock_unparsable(lock: PortfolioLock, clock: FixedClock) -> None:
    plant(lock, clock, age_min=600)
    raw = json.loads(lock.path.read_text())
    raw["extra"] = 1
    lock.path.write_text(json.dumps(raw))
    assert lock.break_stale(15) is None


# -- the real psutil-backed checker -------------------------------------------------------------------------------


def test_proc_checker_sees_this_process_alive() -> None:
    checker = ProcPidChecker()
    assert checker.alive(os.getpid(), checker.start_time(os.getpid()))


def test_proc_checker_rejects_same_pid_with_different_start() -> None:
    checker = ProcPidChecker()
    assert not checker.alive(os.getpid(), checker.start_time(os.getpid()) - timedelta(hours=1))


def test_proc_checker_dead_pid() -> None:
    assert not ProcPidChecker().alive(DEAD_PID, START)


def test_real_lock_roundtrip_with_proc_checker(tmp_path: Path) -> None:
    lk = PortfolioLock(tmp_path / "ws")
    held = lk.acquire("x", 1, 0)
    assert lk.break_stale(0) is None  # we are alive
    assert lk.release(held.token)
