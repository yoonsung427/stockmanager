"""The shared assertions of TS §0.2, by name (A-UNCHANGED, A-NOFILL, A-RECON, A-LOGSYNC, A-LOCKFREE, A-NOTMP, A-AUDIT, A-NOTIFY)."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from stockmanager.domain.docs import PortfolioDoc, ProposalDoc
from stockmanager.store import FileStore
from stockmanager.store.reconcile import LOG_PHRASE, compare_log, read_log_lines, reconcile_portfolio

#: Not part of A-UNCHANGED: run-notes, the alert queue, and lock/state bookkeeping (TS §0.2).
_VOLATILE = ("run-notes", "alerts", "state", "portfolio.lock", ".portfolio.lock.guard")


def workspace_hashes(root: Path, volatile: tuple[str, ...] = _VOLATILE) -> dict[str, str]:
    """SHA-256 of every workspace file except the volatile ones: the left and right side of A-UNCHANGED."""
    out: dict[str, str] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = Path(dirpath) / name
            rel = path.relative_to(root)
            if rel.parts[0] in volatile or rel.name.startswith("portfolio.lock"):
                continue
            out[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


@dataclass(frozen=True)
class Books:
    """What A-NOFILL compares: ``trades[]`` ids, ``trades.log`` line count, ``cash``, ``positions[]``."""

    trade_ids: tuple[str, ...]
    log_lines: int
    cash: Decimal
    positions: tuple[tuple[str, Decimal], ...]


def books(store: FileStore) -> Books:
    """Capture the A-NOFILL tuple from disk."""
    doc = store.load_portfolio()
    return Books(
        tuple(t.trade_id for t in doc.trades),
        len(read_log_lines(store.trades_log_path)),
        doc.cash,
        tuple(sorted((p.ticker, p.shares) for p in doc.positions)),
    )


def assert_nofill(before: Books, store: FileStore) -> None:
    """A-NOFILL."""
    assert books(store) == before


def assert_recon(store: FileStore) -> None:
    """A-RECON: replaying ``trades[]`` from ``start_cash`` reproduces ``positions[]`` and ``cash``."""
    _rebuilt, divergences = reconcile_portfolio(store.load_portfolio())
    assert divergences == []


def assert_logsync(store: FileStore) -> None:
    """A-LOGSYNC: the set of ids in ``trades[]`` equals the set in ``trades.log``, no duplicates."""
    doc = store.load_portfolio()
    cmp = compare_log(doc.trades, read_log_lines(store.trades_log_path))
    assert cmp.divergences == []
    assert cmp.missing_suffix == []


def assert_lockfree(store: FileStore) -> None:
    """A-LOCKFREE."""
    assert not store.lock.is_held()


def assert_notmp(root: Path) -> None:
    """A-NOTMP: no ``*.tmp`` file is left to be read."""
    assert [p for p in root.rglob("*.tmp")] == []


def assert_audit(doc: PortfolioDoc, store: FileStore) -> None:
    """A-AUDIT minus the 06:30 timestamp (see ``test_fill_timestamp_is_session_open``): fields, 4 dp, and a log line each."""
    lines = read_log_lines(store.trades_log_path)
    for t in doc.trades:
        assert t.trade_id and t.price_source in ("yahoo", "finnhub")
        for value in (t.shares, t.fill_price, t.cost):
            assert value == value.quantize(Decimal("0.0001"))
        matching = [line for line in lines if line.startswith(f"{t.trade_id} | ")]
        assert len(matching) == 1 and matching[0].endswith(LOG_PHRASE)


def alerts(root: Path, kind: str | None = None) -> list[dict[str, Any]]:
    """Alerts in ``alerts/pending`` and ``alerts/sent`` (oldest first), optionally of one ``kind``: A-NOTIFY's count."""
    out: list[dict[str, Any]] = []
    for sub in ("pending", "sent"):
        directory = root / "alerts" / sub
        if directory.exists():
            out += [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]
    out.sort(key=lambda a: a["alert_id"])
    return [a for a in out if kind is None or a["kind"] == kind]


def proposal_state(store: FileStore, proposal_id: str) -> str:
    """The persisted state of a proposal."""
    return ProposalDoc.model_validate_json(store.proposal_path(proposal_id).read_text(encoding="utf-8")).state
