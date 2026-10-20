"""``stockmanager doctor`` and the ``ops/`` example files (AS §10.1)."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from stockmanager.cli import main
from stockmanager.ops import doctor as doctor_mod
from stockmanager.ops.doctor import check_crontab, check_launchd, run_doctor

REPO = Path(__file__).resolve().parents[2]
PY = Path("/Users/yoonsung/stockmanager/.venv/bin/python")
WS = Path("/data/ws")
LINE = f"{PY} -m stockmanager tick --workspace {WS}"


def test_crontab_example_is_accepted_once_placeholders_are_filled() -> None:
    text = (REPO / "ops" / "crontab.example").read_text().replace("/path/to/workspace", str(WS))
    assert check_crontab(text, PY, WS).ok


def test_crontab_example_as_shipped_is_rejected_until_edited() -> None:
    assert not check_crontab((REPO / "ops" / "crontab.example").read_text(), PY, WS).ok


def test_plist_example_is_accepted_once_placeholders_are_filled(tmp_path: Path) -> None:
    text = (REPO / "ops" / "com.stockmanager.tick.plist.example").read_text().replace("/path/to/workspace", str(WS))
    path = tmp_path / "com.stockmanager.tick.plist"
    path.write_text(text)
    assert check_launchd(path, PY, WS).ok


@pytest.mark.parametrize(
    ("crontab", "reason"),
    [
        (None, "no crontab"),
        ("", "no crontab"),
        (f"# */5 * * * * {LINE}\n", "no stockmanager tick line"),
        (f"*/5 * * * * {LINE.replace('/data/ws', '/other')}\n", "workspace is /other"),
        (f"*/5 * * * * {LINE.replace('.venv/bin/python', 'bin/python3')}\n", "interpreter is"),
        (f"0 * * * * {LINE}\n", "slower than every 5 minutes"),
        (f"*/5 * * * * {PY} -m stockmanager tick\n", "no --workspace"),
    ],
)
def test_crontab_rejections(crontab: str | None, reason: str) -> None:
    finding = check_crontab(crontab, PY, WS)
    assert not finding.ok
    assert reason in finding.detail


def test_crontab_accepts_redirects_and_workspace_equals_form() -> None:
    line = f"*/5 * * * * {PY} -m stockmanager tick --workspace={WS} >> {WS}/logs/tick.log 2>&1\n"
    assert check_crontab(line, PY, WS).ok


def _plist(tmp_path: Path, **overrides: object) -> Path:
    data: dict[str, object] = {
        "Label": "com.stockmanager.tick",
        "ProgramArguments": [str(PY), "-m", "stockmanager", "tick", "--workspace", str(WS)],
        "StartInterval": 300,
    }
    data.update(overrides)
    path = tmp_path / "com.stockmanager.tick.plist"
    path.write_bytes(plistlib.dumps(data))
    return path


def test_launchd_rejections(tmp_path: Path) -> None:
    assert "not found" in check_launchd(tmp_path / "missing.plist", PY, WS).detail
    assert "workspace is" in check_launchd(_plist(tmp_path, ProgramArguments=[str(PY), "-m", "stockmanager", "tick", "--workspace", "/x"]), PY, WS).detail
    assert "StartInterval" in check_launchd(_plist(tmp_path, StartInterval=3600), PY, WS).detail
    (tmp_path / "bad.plist").write_text("not a plist")
    assert "unreadable" in check_launchd(tmp_path / "bad.plist", PY, WS).detail


def test_run_doctor_healthy_if_either_scheduler_matches(tmp_path: Path) -> None:
    healthy, lines = run_doctor(PY, WS, crontab_reader=lambda: f"*/5 * * * * {LINE}\n", launch_agents_dir=tmp_path)
    assert healthy
    assert any(line.startswith("✅ cron") for line in lines)
    assert any(line.startswith("❌ launchd") for line in lines)

    _plist(tmp_path)
    healthy, _ = run_doctor(PY, WS, crontab_reader=lambda: None, launch_agents_dir=tmp_path)
    assert healthy


def test_run_doctor_unhealthy_when_neither_matches(tmp_path: Path) -> None:
    healthy, lines = run_doctor(PY, WS, crontab_reader=lambda: None, launch_agents_dir=tmp_path)
    assert not healthy
    assert "hermes cron" in lines[-1]


def test_doctor_cli_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(doctor_mod, "read_crontab", lambda: None)
    assert main(["doctor", "--workspace", str(WS), "--python", str(PY)]) == 1
    assert "no scheduler entry" in capsys.readouterr().out

    monkeypatch.setattr(doctor_mod, "read_crontab", lambda: f"*/5 * * * * {LINE}\n")
    assert main(["doctor", "--workspace", str(WS), "--python", str(PY)]) == 0
    assert "a scheduler entry is installed" in capsys.readouterr().out
