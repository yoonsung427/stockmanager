"""File-backed store for the books (AS §3, §8.5). Synchronous; the only code allowed to write ``workspace/``.

Every file is written tmp -> full fsync -> rename -> directory fsync; append-only files use ``O_APPEND`` and fsync
per line. Callers that mutate the books go through ``FileStore.transaction`` so they hold ``portfolio.lock``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from stockmanager.domain.docs import PortfolioDoc
from stockmanager.store import fsutil
from stockmanager.store.lock import Clock, LockMeta, PidChecker, PortfolioLock, now_pt

if TYPE_CHECKING:
    from stockmanager.store.transaction import Transaction

__all__ = ["SUPPORTED_SCHEMA_VERSION", "FileStore", "LockMeta", "LockNotHeld", "SchemaTooNew", "canonical_json"]

SUPPORTED_SCHEMA_VERSION = 1


class SchemaTooNew(Exception):
    """A file carries a newer ``schema_version`` than this build understands; upgrade the app."""


class LockNotHeld(Exception):
    """An operation that requires ``portfolio.lock`` ran while nobody holds it."""


def canonical_json(doc: BaseModel | Mapping[str, Any]) -> str:
    """AS §3.0 canonical JSON: ``indent=2``, model field order, ``ensure_ascii=False``, trailing newline."""
    data = doc.model_dump(mode="json") if isinstance(doc, BaseModel) else doc
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


class FileStore:
    """Reads and writes the files under ``root`` (``workspace/``). Holds no state beyond paths and seams."""

    def __init__(
        self,
        root: Path = Path("workspace"),
        *,
        clock: Clock = now_pt,
        pid_checker: PidChecker | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.root = root
        self.clock = clock
        kwargs: dict[str, Any] = {"clock": clock, "pid_checker": pid_checker}
        if sleep is not None:
            kwargs["sleep"] = sleep
        self.lock = PortfolioLock(root, **kwargs)

    # -- paths ---------------------------------------------------------------------------------------------------

    @property
    def portfolio_path(self) -> Path:
        """``portfolio.json``."""
        return self.root / "portfolio.json"

    @property
    def trades_log_path(self) -> Path:
        """``trades.log``."""
        return self.root / "trades.log"

    @property
    def nav_history_path(self) -> Path:
        """``nav_history.jsonl``."""
        return self.root / "nav_history.jsonl"

    @property
    def acks_path(self) -> Path:
        """``acks.jsonl``."""
        return self.root / "acks.jsonl"

    def proposal_path(self, proposal_id: str) -> Path:
        """``proposals/<proposal_id>.json``."""
        return self._child("proposals", f"{proposal_id}.json")

    def _child(self, directory: str, name: str) -> Path:
        """``root/directory/name``, refusing names that would escape ``directory``."""
        base = self.root / directory
        path = base / name
        if Path(name).is_absolute() or ".." in Path(name).parts or os.path.commonpath([base, path]) != str(base):
            raise ValueError(f"unsafe file name: {name!r}")
        return path

    # -- reads ---------------------------------------------------------------------------------------------------

    def portfolio_exists(self) -> bool:
        """True iff ``portfolio.json`` exists."""
        return self.portfolio_path.exists()

    def load_portfolio(self) -> PortfolioDoc:
        """Read ``portfolio.json``. Raises ``SchemaTooNew`` for a newer ``schema_version``, ``FileNotFoundError``."""
        raw = json.loads(self.portfolio_path.read_text(encoding="utf-8"))
        version = raw.get("schema_version", 1) if isinstance(raw, dict) else 1
        if isinstance(version, int) and version > SUPPORTED_SCHEMA_VERSION:
            raise SchemaTooNew(f"portfolio.json is schema_version {version}; this build understands up to 1")
        return PortfolioDoc.model_validate_json(json.dumps(raw))

    # -- writes --------------------------------------------------------------------------------------------------

    def save_portfolio(self, doc: PortfolioDoc) -> None:
        """Atomically replace ``portfolio.json`` (tmp -> full fsync -> rename -> dir fsync)."""
        fsutil.atomic_write_text(self.portfolio_path, doc.to_canonical_json())

    def append_trades_log(self, lines: list[str]) -> None:
        """Append to ``trades.log`` with ``O_APPEND`` and an fsync per line (leading ``\\n`` after a torn line)."""
        fsutil.append_lines(self.trades_log_path, lines)

    def save_proposal(self, proposal_id: str, doc: BaseModel | Mapping[str, Any]) -> None:
        """Atomically replace ``proposals/<proposal_id>.json``."""
        fsutil.atomic_write_text(self.proposal_path(proposal_id), canonical_json(doc))

    def save_nav_history(self, rows: list[dict[str, Any]]) -> None:
        """Append one JSON line per row to ``nav_history.jsonl``, fsync per line."""
        fsutil.append_lines(self.nav_history_path, [_jsonl(r) for r in rows])

    def save_extras(
        self,
        *,
        evaluations: Mapping[str, str] | None = None,
        acks: Sequence[Mapping[str, Any]] = (),
        run_notes: Mapping[str, str] | None = None,
        dashboard: Mapping[str, str] | None = None,
        alerts: Mapping[str, str] | None = None,
    ) -> None:
        """Write the secondary files; each mapping is relative file name -> text, replaced tmp -> rename.

        ``acks`` are appended to the append-only ``acks.jsonl`` instead (AS §3.11). Names may contain a
        sub-directory (``evaluations/eval-2026-10-24.json``) but may not escape their directory.
        """
        for directory, files in (
            ("evaluations", evaluations),
            ("run-notes", run_notes),
            ("dashboard", dashboard),
            ("alerts/pending", alerts),
        ):
            for name, text in (files or {}).items():
                fsutil.atomic_write_text(self._child(directory, name), text)
        if acks:
            fsutil.append_lines(self.acks_path, [_jsonl(a) for a in acks])

    def cleanup_tmp(self) -> list[Path]:
        """Remove stale ``*.tmp`` files under the workspace. Only valid while holding ``portfolio.lock``."""
        if not self.lock.is_held():
            raise LockNotHeld("cleanup_tmp requires portfolio.lock")
        removed: list[Path] = []
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                path = Path(dirpath) / name
                if name.endswith(".tmp") and not path.is_symlink():
                    path.unlink()
                    removed.append(path)
        return removed

    def transaction(self, job: str, *, max_attempts: int = 3, sleep_seconds: float = 10.0) -> Transaction:
        """Begin a transaction (acquires the lock on ``__enter__``). Jobs: 3 x ~10 s; commands: ~5 s total."""
        from stockmanager.store.transaction import Transaction

        return Transaction(self, job, max_attempts=max_attempts, sleep_seconds=sleep_seconds)


def _jsonl(row: Mapping[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))
