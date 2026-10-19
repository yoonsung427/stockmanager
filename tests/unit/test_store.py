"""FileStore: canonical files, tmp -> fsync -> rename protocol, append rules (AS §3.0, §3.2, §8.5)."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from stockmanager.store import FileStore, LockNotHeld, SchemaTooNew, fsutil
from tests.support.books import FakePidChecker, FixedClock, make_portfolio, make_trade


@pytest.fixture
def store(tmp_path: Path) -> FileStore:
    return FileStore(tmp_path / "ws", clock=FixedClock(), pid_checker=FakePidChecker())


@pytest.fixture
def fsyncs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Record every full_fsync as the kind of thing it hit ('file:<name>' or 'dir')."""
    calls: list[str] = []
    real = fsutil.full_fsync

    def spy(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            calls.append("dir")
        else:
            calls.append(
                "file:" + os.path.basename(fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024).split(b"\0")[0].decode())
            )
        real(fd)

    monkeypatch.setattr(fsutil, "full_fsync", spy)
    yield calls


def test_save_load_portfolio_roundtrip_is_canonical(store: FileStore) -> None:
    doc = make_portfolio([make_trade(1)])
    store.save_portfolio(doc)
    text = store.portfolio_path.read_text(encoding="utf-8")
    assert text == doc.to_canonical_json()
    assert text.endswith("}\n") and text.startswith('{\n  "schema_version": 1')
    assert '"cash": "9000.0000"' in text
    assert store.load_portfolio() == doc


def test_save_portfolio_leaves_no_tmp_and_orders_fsyncs(store: FileStore, fsyncs: list[str]) -> None:
    store.save_portfolio(make_portfolio())
    assert not list(store.root.glob("*.tmp"))
    assert fsyncs == ["file:portfolio.json.tmp", "dir"]  # data flushed before rename, directory after


def test_save_portfolio_replaces_atomically(store: FileStore) -> None:
    store.save_portfolio(make_portfolio())
    seen: list[str] = []
    real_rename = os.rename

    def spy(src: str | Path, dst: str | Path) -> None:
        seen.append(Path(src).name + " -> " + Path(dst).name)
        assert json.loads(Path(src).read_text())["cash"]  # tmp is complete before it is renamed
        real_rename(src, dst)

    os.rename = spy  # type: ignore[assignment]
    try:
        store.save_portfolio(make_portfolio([make_trade(1)]))
    finally:
        os.rename = real_rename
    assert seen == ["portfolio.json.tmp -> portfolio.json"]


def test_load_portfolio_refuses_newer_schema(store: FileStore) -> None:
    store.save_portfolio(make_portfolio())
    raw = json.loads(store.portfolio_path.read_text())
    raw["schema_version"] = 2
    raw["brand_new_field"] = 1
    store.portfolio_path.write_text(json.dumps(raw))
    with pytest.raises(SchemaTooNew):
        store.load_portfolio()


def test_load_portfolio_keeps_decimals_exact(store: FileStore) -> None:
    doc = make_portfolio([make_trade(1, shares="53.3760", price="187.3")])
    store.save_portfolio(doc)
    assert store.load_portfolio().positions[0].cost_basis == Decimal("9997.3248")


def test_append_trades_log_fsyncs_per_line(store: FileStore, fsyncs: list[str]) -> None:
    store.append_trades_log(["a", "b", "c"])
    assert store.trades_log_path.read_text() == "a\nb\nc\n"
    assert fsyncs == ["file:trades.log"] * 3 + ["dir"]


def test_append_trades_log_appends_to_existing(store: FileStore) -> None:
    store.append_trades_log(["a"])
    store.append_trades_log(["b"])
    assert store.trades_log_path.read_text() == "a\nb\n"


def test_append_after_torn_line_adds_leading_newline_and_keeps_torn_bytes(store: FileStore) -> None:
    store.root.mkdir(parents=True)
    store.trades_log_path.write_bytes(b"good\ntorn-li")
    store.append_trades_log(["next"])
    assert store.trades_log_path.read_bytes() == b"good\ntorn-li\nnext\n"


def test_append_rejects_embedded_newline(store: FileStore) -> None:
    with pytest.raises(ValueError):
        store.append_trades_log(["a\nb"])
    assert not store.trades_log_path.exists()


def test_save_proposal_tmp_rename(store: FileStore, fsyncs: list[str]) -> None:
    store.save_proposal("P-2026-10-24-01", {"schema_version": 1, "state": "PENDING", "note": "é"})
    path = store.root / "proposals" / "P-2026-10-24-01.json"
    assert path.read_text(encoding="utf-8") == '{\n  "schema_version": 1,\n  "state": "PENDING",\n  "note": "é"\n}\n'
    assert fsyncs == ["file:P-2026-10-24-01.json.tmp", "dir"]
    assert not list(path.parent.glob("*.tmp"))


def test_save_nav_history_appends_jsonl(store: FileStore) -> None:
    store.save_nav_history([{"date": "2026-10-12", "nav": "50000.0000"}])
    store.save_nav_history([{"date": "2026-10-13", "nav": "50100.0000"}])
    lines = store.nav_history_path.read_text().splitlines()
    assert [json.loads(x)["date"] for x in lines] == ["2026-10-12", "2026-10-13"]


def test_save_extras_routes_each_kind(store: FileStore) -> None:
    store.save_extras(
        evaluations={"eval-2026-10-24.json": "{}\n"},
        acks=[{"kind": "log_line", "line_no": 3}],
        run_notes={"n.md": "note"},
        dashboard={"portfolio.json": "{}\n", "evaluations/eval-2026-10-24.html": "<p>"},
        alerts={"a1.json": "{}\n"},
    )
    r = store.root
    assert (r / "evaluations/eval-2026-10-24.json").exists()
    assert json.loads((r / "acks.jsonl").read_text()) == {"kind": "log_line", "line_no": 3}
    assert (r / "run-notes/n.md").read_text() == "note"
    assert (r / "dashboard/portfolio.json").exists() and (r / "dashboard/evaluations/eval-2026-10-24.html").exists()
    assert (r / "alerts/pending/a1.json").exists()


@pytest.mark.parametrize("name", ["../escape.json", "/abs.json", "a/../../x"])
def test_save_extras_rejects_paths_escaping_directory(store: FileStore, name: str) -> None:
    with pytest.raises(ValueError):
        store.save_extras(dashboard={name: "x"})


def test_cleanup_tmp_requires_lock_and_removes_only_tmp(store: FileStore) -> None:
    (store.root / "proposals").mkdir(parents=True)
    (store.root / "portfolio.json.tmp").write_text("half")
    (store.root / "proposals" / "P-1.json.tmp").write_text("half")
    (store.root / "proposals" / "P-1.json").write_text("keep")
    with pytest.raises(LockNotHeld):
        store.cleanup_tmp()
    lock = store.lock.acquire("test", 1, 0)
    try:
        removed = store.cleanup_tmp()
    finally:
        store.lock.release(lock.token)
    assert len(removed) == 2
    assert [p.name for p in store.root.rglob("*.tmp")] == []
    assert (store.root / "proposals" / "P-1.json").read_text() == "keep"
