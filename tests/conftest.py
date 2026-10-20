"""Shared fixtures. ``world`` is the TS §0.3 harness: temp workspace, fake clock, scripted providers, fault injector."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from stockmanager.store import fsutil
from tests.support.world import World


@pytest.fixture
def fast_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip ``F_FULLFSYNC`` (slow on macOS). Simulated kills are process-level, so durability is not under test."""
    monkeypatch.setattr(fsutil, "full_fsync", lambda fd: None)


@pytest.fixture
def world(tmp_path: Path, fast_fsync: None) -> Iterator[World]:
    """A fresh ``World`` at ``EXEC`` (Mon 2026-10-26 06:35 PT) with an empty workspace."""
    yield World(tmp_path / "ws")
