"""Composition root (AS §1.3): ``create_app`` wiring, config validation, missing-workspace handling."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stockmanager.app import AppContext, ConfigInvalid, WorkspaceMissing, create_app, init_workspace, load_config
from stockmanager.config.model import Config
from stockmanager.evaluation.engine import EvaluationEngine
from stockmanager.execution.engine import ExecutionEngine

PT = ZoneInfo("America/Los_Angeles")


def fixed_clock() -> datetime:
    return datetime(2026, 10, 19, 9, 0, tzinfo=PT)


def make_workspace(tmp_path: Path, **init_kwargs: object) -> Path:
    ws = tmp_path / "ws"
    init_workspace(ws, **init_kwargs)  # type: ignore[arg-type]
    return ws


# -- create_app: missing workspace --------------------------------------------------------------------------------


def test_create_app_missing_workspace_raises_workspace_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(WorkspaceMissing):
        create_app(missing, clock=fixed_clock)


def test_create_app_missing_workspace_message_mentions_init(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(WorkspaceMissing, match="init"):
        create_app(missing, clock=fixed_clock)


# -- init_workspace -------------------------------------------------------------------------------------------


def test_init_workspace_creates_directory_tree(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    for sub in ("proposals", "state", "run-notes", "dashboard", "logs"):
        assert (ws / sub).is_dir()


def test_init_workspace_writes_default_config(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path, start_cash="12345")
    cfg = load_config(ws)
    assert cfg.start_cash == Decimal(12345)


def test_init_workspace_writes_staged_portfolio(tmp_path: Path) -> None:
    from stockmanager.store import FileStore

    ws = make_workspace(tmp_path, start_cash="12345")
    store = FileStore(ws)
    assert store.portfolio_exists()
    portfolio = store.load_portfolio()
    assert portfolio.status == "STAGED"
    assert portfolio.cash == Decimal("12345.0000")


def test_init_workspace_is_idempotent_and_keeps_existing_config(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    init_workspace(ws, start_cash="1000")
    init_workspace(ws, start_cash="999999")  # second call must not overwrite
    cfg = load_config(ws)
    assert cfg.start_cash == Decimal(1000)


def test_init_workspace_sets_restrictive_directory_permissions(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    mode = ws.stat().st_mode & 0o777
    assert mode == 0o700


def test_init_workspace_sets_restrictive_config_file_permissions(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    mode = (ws / "config.yaml").stat().st_mode & 0o777
    assert mode == 0o600


# -- load_config: validation ------------------------------------------------------------------------------------


def test_load_config_defaults_when_no_file(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = load_config(ws)
    assert cfg == Config()


def test_load_config_bad_value_raises_config_invalid(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text("max_position_pct: 500\n", encoding="utf-8")
    with pytest.raises(ConfigInvalid):
        load_config(ws)


def test_load_config_unknown_key_raises_config_invalid(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text("not_a_real_key: 1\n", encoding="utf-8")
    with pytest.raises(ConfigInvalid):
        load_config(ws)


def test_load_config_explicit_config_path_overrides_default(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    other = tmp_path / "other-config.yaml"
    other.write_text("start_cash: 777\n", encoding="utf-8")
    cfg = load_config(ws, other)
    assert cfg.start_cash == Decimal(777)


def test_create_app_invalid_config_raises_config_invalid(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    (ws / "config.yaml").write_text("max_position_pct: 500\n", encoding="utf-8")
    with pytest.raises(ConfigInvalid):
        create_app(ws, clock=fixed_clock)


# -- create_app: wiring ------------------------------------------------------------------------------------------


def test_create_app_wires_store_rooted_at_workspace(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    ctx = create_app(ws, clock=fixed_clock)
    assert ctx.store.root == ws


def test_create_app_wires_clock(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    ctx = create_app(ws, clock=fixed_clock)
    assert ctx.clock() == fixed_clock()


def test_create_app_wires_execution_engine(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    ctx = create_app(ws, clock=fixed_clock)
    assert isinstance(ctx.execution, ExecutionEngine)


def test_create_app_wires_evaluation_engine(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    ctx = create_app(ws, clock=fixed_clock)
    assert isinstance(ctx.evaluation, EvaluationEngine)


def test_create_app_price_providers_match_config_sources(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    ctx = create_app(ws, clock=fixed_clock)
    assert ctx.price_primary.name == ctx.cfg.price_source_primary
    assert ctx.price_fallback.name == ctx.cfg.price_source_fallback


def test_create_app_defaults_to_no_notify_channels_without_discord_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    ws = make_workspace(tmp_path)
    ctx = create_app(ws, clock=fixed_clock)
    assert ctx.notify_channels == []


def test_create_app_wires_discord_channel_when_token_and_channel_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = make_workspace(tmp_path)
    (ws / "config.yaml").write_text("discord:\n  channel_id: '123456'\n", encoding="utf-8")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    ctx = create_app(ws, clock=fixed_clock)
    assert len(ctx.notify_channels) == 1


def test_create_app_calendar_is_holiday_aware(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    ctx = create_app(ws, clock=fixed_clock)
    monday = datetime(2026, 10, 19, 5, 0, tzinfo=PT)  # before open, same day
    opened = ctx.calendar.next_session_open_at_or_after(monday)
    assert opened.weekday() < 5
    friday_before_mlk = datetime(2027, 1, 15, 7, 0, tzinfo=PT)  # Mon 2027-01-18 is a market holiday
    assert ctx.calendar.next_session_open_at_or_after(friday_before_mlk).date() == date(2027, 1, 19)


def test_create_app_command_ctx_shares_store_and_config(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    ctx = create_app(ws, clock=fixed_clock)
    assert ctx.command_ctx.store is ctx.store
    assert ctx.command_ctx.cfg == ctx.cfg


def test_create_app_returns_app_context_instance(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    ctx = create_app(ws, clock=fixed_clock)
    assert isinstance(ctx, AppContext)
