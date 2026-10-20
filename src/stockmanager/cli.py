"""Command-line entry point (AS §1.0, §1.3): the user-facing surface and the cron hook for ``tick``.

Every subcommand resolves a workspace path, builds the composition root via ``app.create_app`` (except ``init``,
which creates the workspace), and prints one reply to stdout -- the same reply text a Discord command would send,
so behaviour matches across surfaces (AS §1.7). Exit code ``0`` means the command ran (even a rejected command
like "no such proposal" is exit ``0``: the operator asked a question and got an answer); exit code ``1`` is
reserved for operational failures (missing workspace, invalid config, unexpected exception).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from stockmanager.app import AppContext, ConfigInvalid, WorkspaceMissing, create_app, init_workspace
from stockmanager.commands.auth import Sender
from stockmanager.commands.handlers import CommandContext, handle

DEFAULT_WORKSPACE_ENV = "STOCKMANAGER_WORKSPACE"


def _default_workspace() -> Path:
    return Path(os.environ.get(DEFAULT_WORKSPACE_ENV, "workspace"))


def _command_context(ctx: AppContext) -> CommandContext:
    return CommandContext(
        store=ctx.store, cfg=ctx.cfg, clock=ctx.clock, calendar=ctx.calendar, price_provider=ctx.price_primary
    )


def _cli_sender(ctx: AppContext) -> Sender:
    return Sender("cli", None, None, ctx.clock())


def _dispatch(ctx: AppContext, text: str) -> str:
    """Run ``text`` through the shared command parser/handler (AS §1.7), identically to a Discord command."""
    return handle(text, _cli_sender(ctx), _command_context(ctx))


# -- subcommands: build their own AppContext from parsed args --------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """``stockmanager init``: create the workspace and a default ``config.yaml`` (UX §2, AS §1.3)."""
    workspace: Path = args.workspace
    init_workspace(workspace, start_cash=args.start_cash)
    print(f"✅ Initialized workspace at {workspace}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """``stockmanager status``: lock-free portfolio and pending-proposal summary (TS CMD-ST05)."""
    ctx = create_app(args.workspace, args.config)
    print(_dispatch(ctx, "STATUS"))
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    """``stockmanager tick``: the cron hook (AS §1.5). Fast; exits immediately if another tick holds the lock."""
    from stockmanager.scheduler.tick import tick as run_tick

    ctx = create_app(args.workspace, args.config)
    result = run_tick(ctx.clock, ctx.store, ctx.cfg, execution=ctx.execution, evaluation=ctx.evaluation, channels=ctx.notify_channels)
    print(f"tick: {'ok' if result == 0 else 'error'}")
    return result


def cmd_news(args: argparse.Namespace) -> int:
    """``stockmanager news``: run the news stage once, standalone, against the current portfolio (AS §7)."""
    ctx = create_app(args.workspace, args.config)
    if not ctx.store.portfolio_exists():
        print("news: no portfolio yet; nothing to evaluate")
        return 0
    portfolio = ctx.store.load_portfolio()
    cycle_id = ctx.clock().date().isoformat()
    result = ctx.run_news_stage(cycle_id, portfolio)
    print(
        f"news: status={result.news_status} facts={len(result.facts)} "
        f"uncovered={', '.join(result.uncovered_tickers) or 'none'}"
    )
    return 0 if result.news_status != "unavailable" else 1


def cmd_report(args: argparse.Namespace) -> int:
    """``stockmanager report``: build and write the read-only dashboard (UX §3.3) to ``workspace/dashboard/``."""
    from stockmanager.dashboard.build import build_dashboard
    from stockmanager.dashboard.render import render_dashboard
    from stockmanager.dashboard.write import write_dashboard

    ctx = create_app(args.workspace, args.config)
    if not ctx.store.portfolio_exists():
        print("report: no portfolio yet")
        return 0
    model = build_dashboard(ctx.store, ctx.clock)
    written = write_dashboard(model, ctx.workspace, render_dashboard, {})
    for path in written:
        print(f"wrote {path}")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    """``stockmanager close``: request (or ``--confirm``) the final close (AS §1.7, UX §8)."""
    ctx = create_app(args.workspace, args.config)
    print(_dispatch(ctx, "CLOSE CONFIRM" if args.confirm else "CLOSE"))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    """``stockmanager set <key> <value>``: change one settable config key (AS §3.6)."""
    ctx = create_app(args.workspace, args.config)
    print(_dispatch(ctx, f"SET {args.key} {args.value}"))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """``stockmanager approve <proposal_id>``: approve an explicit-mode proposal before ``execute_at``."""
    ctx = create_app(args.workspace, args.config)
    print(_dispatch(ctx, f"APPROVE {args.proposal_id}"))
    return 0


def cmd_veto(args: argparse.Namespace) -> int:
    """``stockmanager veto <proposal_id> [reason...]``: veto a proposal (AS §8.4)."""
    ctx = create_app(args.workspace, args.config)
    reason = " ".join(args.reason) if args.reason else ""
    text = f"VETO {args.proposal_id} {reason}".strip()
    print(_dispatch(ctx, text))
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    """``stockmanager pause``: pause the system, cancelling every non-terminal proposal (TS NN-33)."""
    ctx = create_app(args.workspace, args.config)
    print(_dispatch(ctx, "PAUSE"))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """``stockmanager resume``: resume a paused system (cancelled proposals stay cancelled)."""
    ctx = create_app(args.workspace, args.config)
    print(_dispatch(ctx, "RESUME"))
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    """``stockmanager ack <line>``: admin ``ack-log-line`` (AS §1.3, CLI-only, TTY-confirmed per AS §7.5)."""
    ctx = create_app(args.workspace, args.config)
    record = {"acked_at": ctx.clock().isoformat(), "actor": "cli", "line": args.line, "note": args.note or ""}
    ctx.store.save_extras(acks=[record])
    print(f"✅ Acknowledged log line {args.line}")
    return 0


def cmd_help(parser: argparse.ArgumentParser, _args: argparse.Namespace) -> int:
    """``stockmanager help``: print the full command vocabulary (UX §1)."""
    parser.print_help()
    return 0


def cmd_bot(args: argparse.Namespace) -> int:
    """``stockmanager bot``: run the Discord bot daemon (AS §9.1), outside the listed UX vocabulary but needed to
    actually serve it; requires ``DISCORD_BOT_TOKEN`` and ``discord.channel_id`` in config.
    """
    ctx = create_app(args.workspace, args.config)
    if not ctx.discord_bot_token:
        print("❌ DISCORD_BOT_TOKEN is not set (env or secrets file); the bot cannot log in.", file=sys.stderr)
        return 1
    if ctx.discord_channel_id is None:
        print("❌ discord.channel_id is not set in config.yaml.", file=sys.stderr)
        return 1
    from stockmanager.bot.bot import PortfolioBot

    bot = PortfolioBot(ctx.store, ctx.discord_channel_id, ctx.command_ctx, ctx.notify_channels, clock=ctx.clock)
    bot.run(ctx.discord_bot_token)
    return 0


# -- argument parsing --------------------------------------------------------------------------------------------


def _add_workspace_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace", type=Path, default=None, help=f"workspace directory (default: ${DEFAULT_WORKSPACE_ENV} or ./workspace)"
    )


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None, help="config.yaml path (default: <workspace>/config.yaml)")


def build_parser() -> argparse.ArgumentParser:
    """Build the ``stockmanager`` argparse CLI (AS §10 command vocabulary, shared with Discord)."""
    parser = argparse.ArgumentParser(prog="stockmanager")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create a new workspace")
    _add_workspace_arg(p_init)
    p_init.add_argument("--start-cash", default="50000")
    p_init.set_defaults(func=cmd_init)

    for name, func, help_text in [
        ("status", cmd_status, "print portfolio status"),
        ("tick", cmd_tick, "run one scheduler tick"),
        ("news", cmd_news, "run the news stage once against the current portfolio"),
        ("report", cmd_report, "build and write the dashboard"),
        ("pause", cmd_pause, "pause the system"),
        ("resume", cmd_resume, "resume the system"),
    ]:
        p = sub.add_parser(name, help=help_text)
        _add_workspace_arg(p)
        _add_config_arg(p)
        p.set_defaults(func=func)

    p_close = sub.add_parser("close", help="request or confirm the final close")
    _add_workspace_arg(p_close)
    _add_config_arg(p_close)
    p_close.add_argument("--confirm", action="store_true", help="send CLOSE CONFIRM instead of CLOSE")
    p_close.set_defaults(func=cmd_close)

    p_set = sub.add_parser("set", help="SET <key> <value>")
    _add_workspace_arg(p_set)
    _add_config_arg(p_set)
    p_set.add_argument("key")
    p_set.add_argument("value")
    p_set.set_defaults(func=cmd_set)

    p_approve = sub.add_parser("approve", help="APPROVE <proposal_id>")
    _add_workspace_arg(p_approve)
    _add_config_arg(p_approve)
    p_approve.add_argument("proposal_id")
    p_approve.set_defaults(func=cmd_approve)

    p_veto = sub.add_parser("veto", help="VETO <proposal_id> [reason...]")
    _add_workspace_arg(p_veto)
    _add_config_arg(p_veto)
    p_veto.add_argument("proposal_id")
    p_veto.add_argument("reason", nargs="*")
    p_veto.set_defaults(func=cmd_veto)

    p_ack = sub.add_parser("ack", help="admin: acknowledge a trades.log line (CLI-only)")
    _add_workspace_arg(p_ack)
    _add_config_arg(p_ack)
    p_ack.add_argument("line", type=int)
    p_ack.add_argument("--note", default="")
    p_ack.set_defaults(func=cmd_ack)

    p_help = sub.add_parser("help", help="print the full command vocabulary")
    _add_workspace_arg(p_help)
    p_help.set_defaults(func=lambda args: cmd_help(parser, args))

    p_bot = sub.add_parser("bot", help="run the Discord bot daemon")
    _add_workspace_arg(p_bot)
    _add_config_arg(p_bot)
    p_bot.set_defaults(func=cmd_bot)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the stockmanager CLI and return the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.workspace is None:
        args.workspace = _default_workspace()
    try:
        return int(args.func(args))
    except WorkspaceMissing as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    except ConfigInvalid as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
