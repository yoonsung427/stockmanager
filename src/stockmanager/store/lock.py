"""``portfolio.lock``: an existence-based lock that survives crashes, made safe by a ``flock`` guard (AS §3.7, §5.4).

The file's existence means "held". It is broken only when it is older than ``stale_minutes`` AND its owner is dead
(PID gone, or the PID now belongs to a process that started at a different time). A live owner is never broken.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import socket
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import psutil
from pydantic import BaseModel, ConfigDict, ValidationError

from stockmanager.store import fsutil

PT = ZoneInfo("America/Los_Angeles")
LOCK_NAME = "portfolio.lock"
GUARD_NAME = ".portfolio.lock.guard"
DEFAULT_STALE_MINUTES = 15
_START_TOLERANCE = timedelta(seconds=2)

Clock = Callable[[], datetime]


def now_pt() -> datetime:
    """Wall-clock now in PT. Production default for the ``Clock`` seam."""
    return datetime.now(PT)


class LockBusy(Exception):
    """The lock is held (or could not be broken). The job aborts: there is no unlocked fallback."""


class LockMeta(BaseModel):
    """Contents of ``portfolio.lock`` (AS §3.7)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int
    token: str
    pid: int
    proc_start: datetime
    host: str
    job: str
    acquired_at: datetime


@dataclass(frozen=True)
class Lock:
    """A held lock: pass ``token`` to ``release``."""

    token: str
    job: str


class PidChecker(Protocol):
    """Seam for process liveness (AS §12.1)."""

    def alive(self, pid: int, start: datetime) -> bool:
        """True iff ``pid`` is a running process that started at ``start`` (so PID reuse is not mistaken for life)."""
        ...

    def start_time(self, pid: int) -> datetime:
        """Start time of ``pid`` as a PT-aware datetime."""
        ...


class ProcPidChecker:
    """``PidChecker`` backed by psutil."""

    def alive(self, pid: int, start: datetime) -> bool:
        """See ``PidChecker.alive``. A zombie counts as dead."""
        try:
            proc = psutil.Process(pid)
            if proc.status() == psutil.STATUS_ZOMBIE:
                return False
            actual = datetime.fromtimestamp(proc.create_time(), PT)
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True  # exists but unreadable: err on the side of "alive", i.e. never break
        return abs(actual - start) <= _START_TOLERANCE

    def start_time(self, pid: int) -> datetime:
        """See ``PidChecker.start_time``."""
        return datetime.fromtimestamp(psutil.Process(pid).create_time(), PT)


class PortfolioLock:
    """Acquire, release and break ``<root>/portfolio.lock``."""

    def __init__(
        self,
        root: Path,
        *,
        clock: Clock = now_pt,
        pid_checker: PidChecker | None = None,
        sleep: Callable[[float], None] = time.sleep,
        stale_minutes: int = DEFAULT_STALE_MINUTES,
    ) -> None:
        self.root = root
        self.path = root / LOCK_NAME
        self._guard_path = root / GUARD_NAME
        self._clock = clock
        self._pids: PidChecker = pid_checker or ProcPidChecker()
        self._sleep = sleep
        self._stale_minutes = stale_minutes

    def is_held(self) -> bool:
        """True iff ``portfolio.lock`` exists (a dangling symlink counts: it still blocks ``O_EXCL``)."""
        return os.path.lexists(self.path)

    def read_meta(self) -> LockMeta | None:
        """Parse the lock file; ``None`` if absent or unparsable."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        with os.fdopen(fd, "rb") as f:
            raw = f.read()
        try:
            return LockMeta.model_validate_json(raw)
        except ValidationError:
            return None

    def acquire(self, job: str, max_attempts: int = 3, sleep_seconds: float = 10.0) -> Lock:
        """Take the lock, trying ``max_attempts`` times with ``sleep_seconds`` between. Raises ``LockBusy``."""
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        for attempt in range(max_attempts):
            with self._guard():
                lock = self._try_create(job)
            if lock is not None:
                return lock
            if attempt < max_attempts - 1:
                self._sleep(sleep_seconds)
        raise LockBusy(f"portfolio busy: lock held after {max_attempts} attempts")

    def release(self, lock_token: str) -> bool:
        """Unlink the lock iff its token matches ``lock_token``. Never removes a lock we do not own."""
        with self._guard():
            meta = self.read_meta()
            if meta is None or meta.token != lock_token:
                return False
            self.path.unlink()
            fsutil.fsync_dir(self.root)
            return True

    def break_stale(self, stale_minutes: int | None = None) -> str | None:
        """Break the lock if it is stale and its owner is dead. Returns the broken token, else ``None``."""
        with self._guard():
            return self._break_if_stale(self._stale_minutes if stale_minutes is None else stale_minutes)

    @contextlib.contextmanager
    def _guard(self) -> Iterator[None]:
        """Blocking ``flock`` on the guard file: a millisecond-long mutex around check/create/break."""
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._guard_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the flock

    def _try_create(self, job: str) -> Lock | None:
        """Create the lock, breaking a stale one first. Caller holds the guard."""
        lock = self._create(job)
        if lock is not None:
            return lock
        if self._break_if_stale(self._stale_minutes) is None:
            return None
        return self._create(job)

    def _create(self, job: str) -> Lock | None:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            return None
        pid = os.getpid()
        meta = LockMeta(
            schema_version=1,
            token=secrets.token_hex(16),
            pid=pid,
            proc_start=self._pids.start_time(pid),
            host=socket.gethostname(),
            job=job,
            acquired_at=self._clock(),
        )
        try:
            os.write(fd, (json.dumps(meta.model_dump(mode="json"), indent=2) + "\n").encode())
            fsutil.full_fsync(fd)
        except BaseException:
            os.close(fd)
            self.path.unlink(missing_ok=True)
            raise
        os.close(fd)
        fsutil.fsync_dir(self.root)
        return Lock(token=meta.token, job=job)

    def _break_if_stale(self, stale_minutes: int) -> str | None:
        """Caller holds the guard. An unparsable lock is held, never stale (AS §5.4)."""
        meta = self.read_meta()
        if meta is None:
            return None
        if self._clock() - meta.acquired_at <= timedelta(minutes=stale_minutes):
            return None
        if self._pids.alive(meta.pid, meta.proc_start):
            return None
        os.rename(self.path, self.root / f"{LOCK_NAME}.broken.{meta.token}")
        fsutil.fsync_dir(self.root)
        self._write_break_note(meta)
        return meta.token

    def _write_break_note(self, meta: LockMeta) -> None:
        """Log the break in run-notes (AS §5.4)."""
        now = self._clock()
        name = f"{now.strftime('%Y%m%dT%H%M%S')}-lockbreak-{meta.token[:8]}.md"
        body = (
            "---\n"
            "job: lockbreak\n"
            f"run_id: {meta.token[:8]}\n"
            f"started: {now.isoformat()}\n"
            f"ended: {now.isoformat()}\n"
            "outcome: lock_broken\n"
            "late: false\n"
            "---\n\n"
            f"Broke stale `{LOCK_NAME}` held by job `{meta.job}` (pid {meta.pid}, host {meta.host}), "
            f"acquired {meta.acquired_at.isoformat()}; owner process is dead. "
            f"Renamed to `{LOCK_NAME}.broken.{meta.token}`.\n"
        )
        fsutil.atomic_write_text(self.root / "run-notes" / name, body)
