"""``stockmanager doctor`` (AS §10.1): check that something is actually firing ``stockmanager tick``.

A silently disabled cron is otherwise invisible. The check passes if *either* the user's crontab or the launchd
agent ``~/Library/LaunchAgents/com.stockmanager.tick.plist`` runs the expected command on a schedule at least as
frequent as every 5 minutes (``ops/crontab.example``, ``ops/com.stockmanager.tick.plist.example``). A job owned by
Hermes cannot be inspected from here, so it is reported as unverifiable rather than as a pass.
"""

from __future__ import annotations

import plistlib
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

LAUNCHD_LABEL = "com.stockmanager.tick"
#: Schedules that tick at least every 5 minutes (the bot's stall alert fires at 10 min of silence, AS §10.1).
ACCEPTED_CRON_SCHEDULES = ("*/5 * * * *", "*/4 * * * *", "*/3 * * * *", "*/2 * * * *", "*/1 * * * *", "* * * * *")
MAX_START_INTERVAL_S = 300


@dataclass(frozen=True)
class Finding:
    """Result of checking one scheduler: ``ok`` plus a one-line explanation either way."""

    source: str
    ok: bool
    detail: str


def expected_argv(python: Path, workspace: Path) -> list[str]:
    """The argv a scheduler entry must run: ``<python> -m stockmanager tick --workspace <workspace>``."""
    return [str(python), "-m", "stockmanager", "tick", "--workspace", str(workspace)]


def _same_path(a: str, b: Path) -> bool:
    return Path(a).expanduser().absolute() == b.expanduser().absolute()


def _argv_problem(argv: Sequence[str], python: Path, workspace: Path) -> str | None:
    """Why ``argv`` is not the expected tick command, or ``None`` if it is."""
    try:
        i = argv.index("-m")
    except ValueError:
        return "no `-m stockmanager tick` in command"
    if i == 0 or argv[i + 1 : i + 3] != ["stockmanager", "tick"]:
        return "command does not run `-m stockmanager tick`"
    if not _same_path(argv[i - 1], python):
        return f"interpreter is {argv[i - 1]}, expected {python}"
    rest = list(argv[i + 3 :])
    found: str | None = None
    for j, token in enumerate(rest):
        if token == "--workspace" and j + 1 < len(rest):
            found = rest[j + 1]
        elif token.startswith("--workspace="):
            found = token.partition("=")[2]
    if found is None:
        return "no --workspace argument"
    if not _same_path(found, workspace):
        return f"workspace is {found}, expected {workspace}"
    return None


def check_crontab(crontab: str | None, python: Path, workspace: Path) -> Finding:
    """Look for a matching tick line in ``crontab`` (the text of ``crontab -l``; ``None`` if there is none)."""
    if not crontab:
        return Finding("cron", False, "no crontab entries")
    problems: list[str] = []
    for raw in crontab.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "stockmanager" not in line or " tick" not in line:
            continue
        fields = line.split(None, 5)
        if len(fields) < 6:
            continue
        schedule, command = " ".join(fields[:5]), fields[5]
        try:
            argv = shlex.split(command, comments=False)
        except ValueError as exc:
            problems.append(f"unparsable command: {exc}")
            continue
        problem = _argv_problem(argv, python, workspace)
        if problem is None and schedule not in ACCEPTED_CRON_SCHEDULES:
            problem = f"schedule `{schedule}` is slower than every 5 minutes"
        if problem is None:
            return Finding("cron", True, f"`{schedule}` runs {' '.join(expected_argv(python, workspace))}")
        problems.append(problem)
    return Finding("cron", False, "; ".join(problems) if problems else "no stockmanager tick line in crontab")


def check_launchd(plist_path: Path, python: Path, workspace: Path) -> Finding:
    """Validate the launchd agent plist at ``plist_path`` against the expected command and a 5-minute schedule."""
    if not plist_path.exists():
        return Finding("launchd", False, f"{plist_path} not found")
    try:
        data = plistlib.loads(plist_path.read_bytes())
    except (plistlib.InvalidFileException, ValueError, OSError) as exc:
        return Finding("launchd", False, f"{plist_path} is unreadable: {exc}")
    argv = data.get("ProgramArguments")
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return Finding("launchd", False, "ProgramArguments missing or malformed")
    problem = _argv_problem(argv, python, workspace)
    interval = data.get("StartInterval")
    calendar = data.get("StartCalendarInterval")
    if problem is None:
        if isinstance(interval, int) and 0 < interval <= MAX_START_INTERVAL_S:
            pass
        elif isinstance(calendar, list) and len(calendar) >= 12:
            pass  # twelve or more calendar slots an hour == at least every 5 minutes
        else:
            problem = "no StartInterval <= 300 or 12+ StartCalendarInterval entries (every 5 minutes)"
    if problem is not None:
        return Finding("launchd", False, problem)
    return Finding("launchd", True, f"{plist_path} runs {' '.join(expected_argv(python, workspace))}")


def read_crontab() -> str | None:
    """The current user's crontab text, or ``None`` if there is none or ``crontab`` is unavailable."""
    try:
        done = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def run_doctor(
    python: Path,
    workspace: Path,
    *,
    crontab_reader: Callable[[], str | None] | None = None,
    launch_agents_dir: Path | None = None,
) -> tuple[bool, list[str]]:
    """Check the scheduler entries; returns ``(healthy, report lines)``. Healthy means cron OR launchd matches."""
    agents = launch_agents_dir if launch_agents_dir is not None else Path.home() / "Library" / "LaunchAgents"
    findings = [
        check_crontab((crontab_reader or read_crontab)(), python, workspace),
        check_launchd(agents / f"{LAUNCHD_LABEL}.plist", python, workspace),
    ]
    healthy = any(f.ok for f in findings)
    lines = [f"{'✅' if f.ok else '❌'} {f.source}: {f.detail}" for f in findings]
    if healthy:
        lines.append("✅ a scheduler entry is installed")
    else:
        lines.append("❌ no scheduler entry runs `stockmanager tick`; see ops/crontab.example or the launchd plist")
        lines.append("   (a Hermes-owned cron job cannot be inspected from here; confirm it with `hermes cron`)")
    return healthy, lines
