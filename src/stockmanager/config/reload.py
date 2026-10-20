"""Config reload (AS §3.6): read ``config.yaml`` -> validate -> mirror; alert once per distinct bad hash."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from stockmanager.config.model import Config
from stockmanager.notify.outbox import enqueue
from stockmanager.notify.templates import config_error
from stockmanager.store import FileStore
from stockmanager.store.fsutil import atomic_write_text
from stockmanager.store.lock import Clock, LockBusy

CONFIG_STATUS_REL_PATH = "state/config_status.json"


def _config_path(root: Path) -> Path:
    return root / "config.yaml"


def _status_path(root: Path) -> Path:
    return root / CONFIG_STATUS_REL_PATH


def _mirrored_config(store: FileStore) -> Config | None:
    """``portfolio.json.config``, the last-good mirror, if present and still valid."""
    if not store.portfolio_exists():
        return None
    data = store.load_portfolio().config
    if not data:
        return None
    try:
        return Config.model_validate(data)
    except ValidationError:
        return None


def reload(store: FileStore, clock: Clock, fallback: Config | None = None) -> Config:
    """Read ``config.yaml``; valid -> mirror it into ``portfolio.json.config``; invalid -> keep running on the mirror.

    Alerts once per distinct bad SHA-256 (tracked in ``state/config_status.json``, cleared on the next valid read).
    Returns the config now in force: the freshly validated file, the last-good mirror, ``fallback`` (the caller's
    previously-in-force config) if given, or ``Config()`` as a last resort.
    """
    default = fallback if fallback is not None else Config()
    path = _config_path(store.root)
    if not path.exists():
        return _mirrored_config(store) or default
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    try:
        data = yaml.safe_load(raw.decode("utf-8")) or {}
        if not isinstance(data, dict):
            raise TypeError("config.yaml is not a mapping")
        cfg = Config.model_validate(data)
    except (yaml.YAMLError, ValidationError, TypeError, UnicodeDecodeError) as exc:
        _alert_once(store, sha, str(exc))
        return _mirrored_config(store) or default
    _status_path(store.root).unlink(missing_ok=True)
    _mirror(store, cfg)
    return cfg


def _mirror(store: FileStore, cfg: Config) -> None:
    """Write ``cfg`` into ``portfolio.json.config`` if it differs, without blocking the tick on a busy lock."""
    if not store.portfolio_exists():
        return
    dumped = cfg.model_dump(mode="json")
    if store.load_portfolio().config == dumped:
        return
    try:
        with store.transaction("config:reload", max_attempts=1, sleep_seconds=0) as txn:
            txn.stage_portfolio(txn.state.model_copy(update={"config": dumped}))
    except LockBusy:
        pass  # another job holds the lock; the next reload mirrors it


def _alert_once(store: FileStore, sha: str, error: str) -> None:
    """Raise ``config_error`` only the first time this ``sha`` is seen (AS §3.6)."""
    status_path = _status_path(store.root)
    if status_path.exists():
        try:
            if json.loads(status_path.read_text(encoding="utf-8")).get("bad_sha") == sha:
                return
        except ValueError:
            pass
    atomic_write_text(status_path, json.dumps({"bad_sha": sha, "error": error}, indent=2, ensure_ascii=False) + "\n")
    enqueue(config_error(sha), store)
