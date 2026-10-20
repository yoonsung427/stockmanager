"""CLI command handlers (cli.py's ``cmd_*`` functions), tested in isolation with a ``SimpleNamespace`` for args.

Each handler is called directly (bypassing argparse) so these tests focus on handler behaviour -- what it reads
from ``args``, how it builds its reply, and its exit code -- independent of argument parsing (covered by
``test_cli.py``) and independent of the underlying command-parser/auth/transition logic (covered by
``tests/unit/test_commands.py`` against ``commands.handlers``). External dependencies (network news fetchers,
Discord) are stubbed via monkeypatching ``stockmanager.cli.create_app`` or environment variables so these tests
never touch the network.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from stockmanager import cli
from stockmanager.app import create_app, init_workspace
from stockmanager.domain.docs import PortfolioDoc
from stockmanager.news import NewsResult, NewsStats
from stockmanager.store import FileStore
from tests.support.proposals import make_proposal


def make_workspace(tmp_path: Path, start_cash: str = "10000") -> Path:
    ws = tmp_path / "ws"
    init_workspace(ws, start_cash=start_cash)
    return ws


def set_status(ws: Path, status: str) -> None:
    store = FileStore(ws)
    portfolio = store.load_portfolio()
    store.save_portfolio(portfolio.model_copy(update={"status": status}))


def args(**kwargs: Any) -> argparse.Namespace:
    """A minimal ``argparse.Namespace`` stand-in: every handler only reads named attributes off ``args``."""
    base: dict[str, Any] = {"workspace": None, "config": None}
    base.update(kwargs)
    return argparse.Namespace(**base)


# -- cmd_init -------------------------------------------------------------------------------------------------


def test_cmd_init_creates_workspace_and_returns_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = tmp_path / "ws"
    code = cli.cmd_init(args(workspace=ws, start_cash="5000"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Initialized workspace" in out
    assert ws.is_dir()


def test_cmd_init_passes_start_cash_through(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    cli.cmd_init(args(workspace=ws, start_cash="42000"))
    assert FileStore(ws).load_portfolio().cash.normalize() == 42000


# -- cmd_status -------------------------------------------------------------------------------------------------


def test_cmd_status_reports_portfolio_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    code = cli.cmd_status(args(workspace=ws))
    out = capsys.readouterr().out
    assert code == 0
    assert "STATUS: STAGED" in out


def test_cmd_status_is_read_only_no_lock_file_created(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    cli.cmd_status(args(workspace=ws))
    assert not (ws / "portfolio.lock").exists()


# -- cmd_tick -------------------------------------------------------------------------------------------------


def test_cmd_tick_returns_zero_and_prints_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    code = cli.cmd_tick(args(workspace=ws))
    out = capsys.readouterr().out
    assert code == 0
    assert "tick: ok" in out


def test_cmd_tick_marks_scheduler_state(tmp_path: Path) -> None:
    from stockmanager.scheduler import state as scheduler_state_mod

    ws = make_workspace(tmp_path)
    cli.cmd_tick(args(workspace=ws))
    assert scheduler_state_mod.load(ws).last_tick is not None


# -- cmd_news -------------------------------------------------------------------------------------------------


def test_cmd_news_no_portfolio_short_circuits(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    code = cli.cmd_news(args(workspace=ws))
    out = capsys.readouterr().out
    assert code == 0
    assert "no portfolio yet" in out


def test_cmd_news_reports_stubbed_result_without_touching_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = make_workspace(tmp_path)
    real_ctx = create_app(ws)
    stub_result = NewsResult(cycle_id="2026-10-19", news_status="ok", facts=[], uncovered_tickers=["AAPL"], stats=NewsStats())

    class StubCtx:
        store = real_ctx.store
        clock = staticmethod(real_ctx.clock)

        def run_news_stage(self, cycle_id: str, portfolio: PortfolioDoc) -> NewsResult:
            return stub_result

    monkeypatch.setattr(cli, "create_app", lambda *a, **kw: StubCtx())
    code = cli.cmd_news(args(workspace=ws))
    out = capsys.readouterr().out
    assert code == 0
    assert "status=ok" in out
    assert "AAPL" in out


def test_cmd_news_returns_nonzero_when_news_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = make_workspace(tmp_path)
    real_ctx = create_app(ws)
    stub_result = NewsResult(cycle_id="2026-10-19", news_status="unavailable", facts=[], uncovered_tickers=[], stats=NewsStats())

    class StubCtx:
        store = real_ctx.store
        clock = staticmethod(real_ctx.clock)

        def run_news_stage(self, cycle_id: str, portfolio: PortfolioDoc) -> NewsResult:
            return stub_result

    monkeypatch.setattr(cli, "create_app", lambda *a, **kw: StubCtx())
    code = cli.cmd_news(args(workspace=ws))
    assert code == 1


# -- cmd_report -----------------------------------------------------------------------------------------------


def test_cmd_report_no_portfolio_prints_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    code = cli.cmd_report(args(workspace=ws))
    out = capsys.readouterr().out
    assert code == 0
    assert "no portfolio yet" in out


def test_cmd_report_writes_dashboard_and_prints_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    code = cli.cmd_report(args(workspace=ws))
    out = capsys.readouterr().out
    assert code == 0
    assert "wrote" in out
    assert (ws / "dashboard").exists()


# -- cmd_close -------------------------------------------------------------------------------------------------


def test_cmd_close_without_confirm_sends_close(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    set_status(ws, "LIVE")
    code = cli.cmd_close(args(workspace=ws, confirm=False))
    out = capsys.readouterr().out
    assert code == 0
    assert "Close requested" in out


def test_cmd_close_with_confirm_but_no_pending_close_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    set_status(ws, "LIVE")
    code = cli.cmd_close(args(workspace=ws, confirm=True))
    out = capsys.readouterr().out
    assert code == 0
    assert "No pending CLOSE request" in out


# -- cmd_set --------------------------------------------------------------------------------------------------


def test_cmd_set_valid_key_updates_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    code = cli.cmd_set(args(workspace=ws, key="max_position_pct", value="40"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Set max_position_pct = 40" in out


def test_cmd_set_blocked_prefix_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    code = cli.cmd_set(args(workspace=ws, key="discord.channel_id", value="1"))
    out = capsys.readouterr().out
    assert code == 0
    assert "❌" in out


# -- cmd_approve / cmd_veto -------------------------------------------------------------------------------------


def test_cmd_approve_explicit_mode_proposal_succeeds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    store = FileStore(ws)
    future = datetime.now(UTC) + timedelta(days=365)
    proposal = make_proposal(state="VETO_WINDOW", approval_mode="explicit", execute_at=future)
    store.save_proposal(proposal.proposal_id, proposal)
    code = cli.cmd_approve(args(workspace=ws, proposal_id=proposal.proposal_id))
    out = capsys.readouterr().out
    assert code == 0
    assert f"Approved {proposal.proposal_id}" in out


def test_cmd_veto_rejects_malformed_proposal_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    code = cli.cmd_veto(args(workspace=ws, proposal_id="not-a-real-id", reason=[]))
    out = capsys.readouterr().out
    assert code == 0
    assert "valid proposal ID" in out


def test_cmd_veto_empty_reason_list_omits_trailing_space(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    store = FileStore(ws)
    proposal = make_proposal(state="PROPOSED")
    store.save_proposal(proposal.proposal_id, proposal)
    code = cli.cmd_veto(args(workspace=ws, proposal_id=proposal.proposal_id, reason=[]))
    out = capsys.readouterr().out
    assert code == 0
    assert f"Vetoed {proposal.proposal_id}." in out


# -- cmd_pause / cmd_resume -------------------------------------------------------------------------------------


def test_cmd_pause_cancels_pending_proposals(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    set_status(ws, "LIVE")
    store = FileStore(ws)
    proposal = make_proposal(state="PROPOSED")
    store.save_proposal(proposal.proposal_id, proposal)
    code = cli.cmd_pause(args(workspace=ws))
    out = capsys.readouterr().out
    assert code == 0
    assert "paused" in out
    reloaded = json.loads(store.proposal_path(proposal.proposal_id).read_text())
    assert reloaded["state"] == "CANCELLED_PAUSED"


def test_cmd_resume_requires_paused_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)  # STAGED, not PAUSED
    code = cli.cmd_resume(args(workspace=ws))
    out = capsys.readouterr().out
    assert code == 0
    assert "❌" in out


# -- cmd_ack --------------------------------------------------------------------------------------------------


def test_cmd_ack_writes_expected_fields(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    code = cli.cmd_ack(args(workspace=ws, line=12, note="rebuilt"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Acknowledged log line 12" in out
    row = json.loads((ws / "acks.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row == {"acked_at": row["acked_at"], "actor": "cli", "line": 12, "note": "rebuilt"}


def test_cmd_ack_defaults_note_to_empty_string(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    cli.cmd_ack(args(workspace=ws, line=1, note=""))
    row = json.loads((ws / "acks.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["note"] == ""


# -- cmd_help -------------------------------------------------------------------------------------------------


def test_cmd_help_prints_parser_help(capsys: pytest.CaptureFixture[str]) -> None:
    parser = cli.build_parser()
    code = cli.cmd_help(parser, args())
    out = capsys.readouterr().out
    assert code == 0
    assert "stockmanager" in out
