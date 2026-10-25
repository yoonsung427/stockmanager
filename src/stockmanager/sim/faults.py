"""``FaultInjector``: simulate a process kill at a named step of the SS §7.1 write order (TS §3.3).

A kill is ``SimulatedKill``, a ``BaseException`` so no ``except Exception`` in the code under test swallows it. A dead
process cannot clean up, so once a kill has fired ``store.lock.release`` becomes a no-op: ``portfolio.lock`` stays
behind exactly as it would after ``kill -9``. The injector wraps one ``FileStore`` *instance*; "restarting" is
building a fresh ``FileStore`` on the same root.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from stockmanager.store import FileStore, fsutil

#: TS §3.3 crash points, in write order.
CRASH_POINTS: Final[tuple[str, ...]] = (
    "after_lock",  # CP0: lock held, nothing written
    "tmp_written",  # CP1: portfolio.json.tmp complete, not renamed
    "after_portfolio_rename",  # CP2: portfolio.json has the fills; trades.log untouched
    "mid_log_append",  # CP3: first of several log lines written (needs a multi-line append)
    "after_log",  # CP4: portfolio.json and trades.log done; proposal state not updated
    "after_proposal_state",  # CP5: proposal updated; run-notes, dashboard, alerts missing
    "after_runnotes",  # CP6: run-notes done; dashboard and alerts missing
    "torn_log_line",  # CP7: partial last line in trades.log
)


class SimulatedKill(BaseException):
    """The process died at ``point``. Derives from ``BaseException`` so only the test harness catches it."""

    def __init__(self, point: str) -> None:
        super().__init__(point)
        self.point = point


class FaultInjector:
    """Arms one crash point on one ``FileStore``. Use ``crash_at`` as a context manager around the job."""

    def __init__(self, store: FileStore) -> None:
        self.store = store
        self.fired = False
        self.point: str | None = None

    @contextmanager
    def crash_at(self, point: str) -> Iterator[FaultInjector]:
        """Arm ``point`` for the ``with`` body; the caller catches ``SimulatedKill`` (or checks ``fired``)."""
        if point not in CRASH_POINTS:
            raise ValueError(f"unknown crash point {point!r}")
        self.point, self.fired = point, False
        undo: list[Callable[[], None]] = []
        try:
            self._install(point, undo)
            yield self
        finally:
            for restore in reversed(undo):
                restore()

    # -- internals -----------------------------------------------------------------------------------------------

    def _kill(self) -> None:
        assert self.point is not None
        self.fired = True
        raise SimulatedKill(self.point)

    def _wrap(self, undo: list[Callable[[], None]], name: str, make: Callable[[Any], Callable[..., Any]]) -> None:
        owner: Any = self.store.lock if name == "release" else self.store
        original = getattr(owner, name)
        setattr(owner, name, make(original))
        undo.append(lambda: delattr(owner, name))  # drop the instance attribute; the class method shows through again

    def _install(self, point: str, undo: list[Callable[[], None]]) -> None:
        def dead_release(original: Callable[..., bool]) -> Callable[..., bool]:
            def release(token: str) -> bool:
                return False if self.fired else original(token)  # a dead process never unlinks its lock

            return release

        self._wrap(undo, "release", dead_release)

        if point == "after_lock":
            self._wrap(undo, "cleanup_tmp", lambda original: self._before(original))
        elif point == "tmp_written":
            self._patch_atomic_write(undo)
        elif point == "after_portfolio_rename":
            self._wrap(undo, "save_portfolio", lambda original: self._after(original))
        elif point in ("mid_log_append", "torn_log_line"):
            self._wrap(undo, "append_trades_log", lambda original: self._partial_append(original, point))
        elif point == "after_log":
            self._wrap(undo, "append_trades_log", lambda original: self._after(original))
        elif point == "after_proposal_state":
            self._wrap(undo, "save_proposal", lambda original: self._after(original))
        elif point == "after_runnotes":
            self._wrap(undo, "save_extras", lambda original: self._before_dashboard(original))

    def _before(self, original: Callable[..., Any]) -> Callable[..., Any]:
        def run(*args: Any, **kwargs: Any) -> Any:
            self._kill()

        return run

    def _after(self, original: Callable[..., Any]) -> Callable[..., Any]:
        def run(*args: Any, **kwargs: Any) -> Any:
            original(*args, **kwargs)
            self._kill()

        return run

    def _before_dashboard(self, original: Callable[..., Any]) -> Callable[..., Any]:
        """Kill when the commit reaches the dashboard step, i.e. after run-notes (and everything before) is on disk."""

        def run(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("dashboard") is not None:
                self._kill()
            return original(*args, **kwargs)

        return run

    def _partial_append(self, original: Callable[..., Any], point: str) -> Callable[..., Any]:
        def run(lines: Sequence[str]) -> None:
            if point == "mid_log_append":
                if len(lines) < 2:
                    original(lines)  # a single-fill proposal has no "middle": the point never fires
                    return
                original(list(lines[:1]))
            else:  # torn_log_line: the first bytes of the first line reach the file, no newline, no fsync
                path = self.store.trades_log_path
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, lines[0][: len(lines[0]) // 2].encode("utf-8"))
                finally:
                    os.close(fd)
            self._kill()

        return run

    def _patch_atomic_write(self, undo: list[Callable[[], None]]) -> None:
        real = fsutil.atomic_write_bytes
        portfolio = self.store.portfolio_path

        def atomic_write_bytes(path: Path, data: bytes) -> None:
            if path != portfolio:
                real(path, data)
                return
            fsutil.tmp_path(path).write_bytes(data)  # the .tmp is complete; the rename never happens
            self._kill()

        fsutil.atomic_write_bytes = atomic_write_bytes
        undo.append(lambda: setattr(fsutil, "atomic_write_bytes", real))



@contextmanager
def sqlite_down() -> Iterator[None]:
    """Every ``sqlite3.connect`` raises for the ``with`` body, as an unavailable event database would (v2 R4).

    Code that must survive the outage (outbox enqueue, dedupe, delivery) is exercised inside it; the atomic-file
    store never opens SQLite, so it must be unaffected.
    """
    real = sqlite3.connect

    def refuse(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file (simulated outage)")

    sqlite3.connect = refuse  # type: ignore[assignment]
    try:
        yield
    finally:
        sqlite3.connect = real
