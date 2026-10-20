"""CLI subcommands (AS §10): init, status, tick, news, report, close, set, approve, veto, pause, resume, ack.

These exercise ``stockmanager.cli.main`` end to end through ``capsys`` (TS CMD-* via the CLI surface). ``tick``
and ``news`` are only tested against an empty/no-portfolio workspace here so they never reach the network; the
underlying pipeline logic is covered by ``test_tick.py``, the news unit tests, and ``test_commands.py``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from stockmanager.app import init_workspace
from stockmanager.cli import main
from stockmanager.store import FileStore


def make_workspace(tmp_path: Path, start_cash: str = "10000") -> Path:
    ws = tmp_path / "ws"
    init_workspace(ws, start_cash=start_cash)
    return ws


def set_status(ws: Path, status: str) -> None:
    store = FileStore(ws)
    portfolio = store.load_portfolio()
    store.save_portfolio(portfolio.model_copy(update={"status": status}))


# -- init -----------------------------------------------------------------------------------------------------


def test_init_creates_workspace_and_prints_confirmation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = tmp_path / "ws"
    code = main(["init", "--workspace", str(ws), "--start-cash", "5000"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Initialized workspace" in out
    assert (ws / "config.yaml").exists()


def test_init_respects_start_cash(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    main(["init", "--workspace", str(ws), "--start-cash", "99999"])
    store = FileStore(ws)
    assert store.load_portfolio().cash == Decimal("99999.0000")


# -- status -----------------------------------------------------------------------------------------------------


def test_status_prints_staged_for_freshly_initialized_workspace(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    code = main(["status", "--workspace", str(ws)])
    out = capsys.readouterr().out
    assert code == 0
    assert "STAGED" in out


def test_status_shows_pending_proposals(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.support.proposals import make_proposal

    ws = make_workspace(tmp_path)
    set_status(ws, "LIVE")
    store = FileStore(ws)
    proposal = make_proposal(state="PROPOSED")
    store.save_proposal(proposal.proposal_id, proposal)
    out = capsys.readouterr().out  # discard init output
    main(["status", "--workspace", str(ws)])
    out = capsys.readouterr().out
    assert proposal.proposal_id in out


def test_status_missing_workspace_exits_with_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["status", "--workspace", str(tmp_path / "nope")])
    err = capsys.readouterr().err
    assert code == 1
    assert "workspace not found" in err


# -- tick -----------------------------------------------------------------------------------------------------


def test_tick_on_empty_workspace_returns_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    code = main(["tick", "--workspace", str(ws)])
    out = capsys.readouterr().out
    assert code == 0
    assert "tick: ok" in out


def test_tick_updates_scheduler_last_tick(tmp_path: Path) -> None:
    from stockmanager.scheduler import state as scheduler_state_mod

    ws = make_workspace(tmp_path)
    main(["tick", "--workspace", str(ws)])
    state = scheduler_state_mod.load(ws)
    assert state.last_tick is not None


def test_tick_missing_workspace_exits_1(tmp_path: Path) -> None:
    code = main(["tick", "--workspace", str(tmp_path / "nope")])
    assert code == 1


# -- news -----------------------------------------------------------------------------------------------------


def test_news_no_portfolio_reports_nothing_to_evaluate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    code = main(["news", "--workspace", str(ws)])
    out = capsys.readouterr().out
    assert code == 0
    assert "no portfolio yet" in out


# -- report ---------------------------------------------------------------------------------------------------


def test_report_no_portfolio_prints_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    code = main(["report", "--workspace", str(ws)])
    out = capsys.readouterr().out
    assert code == 0
    assert "no portfolio yet" in out


def test_report_writes_dashboard_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    capsys.readouterr()
    code = main(["report", "--workspace", str(ws)])
    out = capsys.readouterr().out
    assert code == 0
    assert "wrote" in out
    assert (ws / "dashboard" / "portfolio.html").exists()


# -- close ----------------------------------------------------------------------------------------------------


def test_close_on_staged_portfolio_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    code = main(["close", "--workspace", str(ws)])
    out = capsys.readouterr().out
    assert code == 0  # the command ran; the reply carries the rejection
    assert "❌" in out


def test_close_request_on_live_portfolio_succeeds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    set_status(ws, "LIVE")
    code = main(["close", "--workspace", str(ws)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Close requested" in out


# -- set ------------------------------------------------------------------------------------------------------


def test_set_valid_key_succeeds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    code = main(["set", "max_position_pct", "25", "--workspace", str(ws)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Set max_position_pct = 25" in out


def test_set_unknown_key_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    code = main(["set", "not_a_real_key", "1", "--workspace", str(ws)])
    out = capsys.readouterr().out
    assert code == 0
    assert "❌" in out


# -- approve / veto ---------------------------------------------------------------------------------------------


def test_approve_unknown_proposal_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    code = main(["approve", "P-2026-10-19-01", "--workspace", str(ws)])
    out = capsys.readouterr().out
    assert code == 0
    assert "No such proposal" in out


def test_veto_known_proposal_succeeds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.support.proposals import make_proposal

    ws = make_workspace(tmp_path)
    store = FileStore(ws)
    proposal = make_proposal(state="PROPOSED")
    store.save_proposal(proposal.proposal_id, proposal)
    capsys.readouterr()
    code = main(["veto", proposal.proposal_id, "too", "risky", "--workspace", str(ws)])
    out = capsys.readouterr().out
    assert code == 0
    assert f"Vetoed {proposal.proposal_id}" in out


def test_veto_reason_is_joined_into_one_string(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.support.proposals import make_proposal

    ws = make_workspace(tmp_path)
    store = FileStore(ws)
    proposal = make_proposal(state="PROPOSED")
    store.save_proposal(proposal.proposal_id, proposal)
    main(["veto", proposal.proposal_id, "too", "risky", "right", "now", "--workspace", str(ws)])
    reloaded = json.loads(store.proposal_path(proposal.proposal_id).read_text())
    assert reloaded["transitions"][-1]["cause"] == "too risky right now"


# -- pause / resume -----------------------------------------------------------------------------------------------


def test_pause_then_resume_round_trips_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    set_status(ws, "LIVE")
    capsys.readouterr()
    code_pause = main(["pause", "--workspace", str(ws)])
    paused_out = capsys.readouterr().out
    code_resume = main(["resume", "--workspace", str(ws)])
    resumed_out = capsys.readouterr().out
    assert code_pause == 0 and "paused" in paused_out
    assert code_resume == 0 and "resumed" in resumed_out
    assert FileStore(ws).load_portfolio().status == "LIVE"


def test_pause_on_staged_portfolio_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    code = main(["pause", "--workspace", str(ws)])
    out = capsys.readouterr().out
    assert code == 0
    assert "❌" in out


# -- ack ------------------------------------------------------------------------------------------------------


def test_ack_appends_a_record_to_acks_jsonl(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    code = main(["ack", "7", "--note", "torn line repaired", "--workspace", str(ws)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Acknowledged log line 7" in out
    rows = [json.loads(line) for line in (ws / "acks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["line"] == 7
    assert rows[0]["note"] == "torn line repaired"
    assert rows[0]["actor"] == "cli"


# -- help / argument parsing -------------------------------------------------------------------------------------


def test_help_prints_command_vocabulary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["help"])
    out = capsys.readouterr().out
    assert code == 0
    assert "status" in out
    assert "veto" in out


def test_main_without_subcommand_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code != 0


def test_workspace_env_var_used_when_no_flag_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    ws = make_workspace(tmp_path)
    monkeypatch.setenv("STOCKMANAGER_WORKSPACE", str(ws))
    capsys.readouterr()
    code = main(["status"])
    out = capsys.readouterr().out
    assert code == 0
    assert "STAGED" in out
