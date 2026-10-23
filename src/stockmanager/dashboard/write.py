"""Write the rendered dashboard to ``workspace/dashboard/`` (AS §9.3, §8.5 row 6).

Every file is replaced atomically (tmp -> fsync -> rename -> directory fsync) via ``store.fsutil``. This module
only writes under ``dashboard/`` (TS DASH-24); it never reads or mutates the books.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from stockmanager.dashboard.model import DashboardModel
from stockmanager.store import fsutil

__all__ = ["model_as_dict", "write_dashboard"]


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value)!r}")


@contextmanager
def _dashboard_lock(dashboard_dir: Path) -> Iterator[None]:
    """Serialize dashboard writers: they run post-commit, outside the store lock, and share ``<name>.tmp`` paths."""
    fd = os.open(dashboard_dir / ".write.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the flock


def model_as_dict(model: DashboardModel) -> dict[str, Any]:
    """``DashboardModel`` as a plain, JSON-ready dict (decimals and dates as strings)."""
    return asdict(model)


def write_dashboard(
    model: DashboardModel,
    workspace: Path,
    render_fn: Callable[[DashboardModel], str],
    eval_pages: Mapping[str, str],
) -> list[Path]:
    """Render and atomically write ``dashboard/portfolio.{html,json}`` and any new evaluation pages.

    ``eval_pages`` maps an evaluation page file name (e.g. ``eval-2026-10-24.html``) to its rendered HTML;
    callers render only evaluations that are new or changed (AS §9.3). Returns every path written.
    """
    dashboard_dir = workspace / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)

    html_text = render_fn(model)
    sidecar_text = json.dumps(model_as_dict(model), ensure_ascii=False, allow_nan=False, default=_json_default)

    with _dashboard_lock(dashboard_dir):
        for stale in dashboard_dir.rglob("*.tmp"):  # left by a crashed writer; safe to drop while holding the lock
            stale.unlink(missing_ok=True)
        html_path = dashboard_dir / "portfolio.html"
        json_path = dashboard_dir / "portfolio.json"
        fsutil.atomic_write_text(html_path, html_text)
        fsutil.atomic_write_text(json_path, sidecar_text)
        written = [html_path, json_path]

        evaluations_dir = dashboard_dir / "evaluations"
        for name, text in eval_pages.items():
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError(f"unsafe evaluation page name: {name!r}")
            path = evaluations_dir / name
            fsutil.atomic_write_text(path, text)
            written.append(path)

        return written
