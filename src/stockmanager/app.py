"""Composition root (AS §1.3): the only place that wires ports to real adapters.

``create_app`` builds everything the CLI, the cron tick and the Discord bot need from a workspace path: the
``FileStore``, the live ``Config``, the clock, the session calendar, the (primary, fallback) price providers, the
``ExecutionEngine``, the news-stage inputs (config, fetchers, extractor), the ``EvaluationEngine``, the notification
channels and the ``CommandContext`` shared by the CLI and the bot. It never prompts and never guesses — a missing
workspace or a config that fails validation raises immediately so the caller can report it (AS §1.5, §3.6).
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import partial
from pathlib import Path

import yaml
from pydantic import ValidationError

from stockmanager.adapters.calendar import JsonCalendar
from stockmanager.adapters.discord_rest import DiscordRESTChannel
from stockmanager.adapters.finnhub import FinnhubPriceProvider
from stockmanager.adapters.hermes_mainchat import HermesMainchatChannel
from stockmanager.adapters.symbol_verifier import YahooSymbolVerifier
from stockmanager.adapters.yfinance_lookup import search_symbols
from stockmanager.adapters.yfinance_provider import YfinancePriceProvider, regular_close
from stockmanager.commands import CommandContext
from stockmanager.config.model import Config
from stockmanager.domain.docs import PortfolioDoc
from stockmanager.domain.ports import PriceProvider, SessionCalendar, SymbolVerifier
from stockmanager.evaluation.engine import EvaluationEngine
from stockmanager.evaluation.input_builder import make_reasoner_input_builder
from stockmanager.evaluation.reasoner import Reasoner, build_reasoner
from stockmanager.evaluation.reconfirm import ReconfirmEngine
from stockmanager.evaluation.thesis_checker import ThesisReview
from stockmanager.execution.engine import ExecutionEngine
from stockmanager.jobs.common import make_news_lookup
from stockmanager.jobs.digest import run_digest
from stockmanager.jobs.price_check import run_price_check
from stockmanager.jobs.stock_query import StockQuery
from stockmanager.llm.anthropic import AnthropicLLMBackend
from stockmanager.llm.openai_compat import OpenAILLMBackend
from stockmanager.llm.summary import generate_summary
from stockmanager.news import NewsResult
from stockmanager.news.config import NewsConfig
from stockmanager.news.extract import Extractor, build_extractor
from stockmanager.news.fetchers.finnhub import FinnhubNewsFetcher
from stockmanager.news.fetchers.searxng import SearxngFetcher
from stockmanager.news.fetchers.yahoo_rss import YahooRssFetcher
from stockmanager.news.pipeline import run_news_stage
from stockmanager.notify.deliver import Channel
from stockmanager.notify.templates import use_catalogue
from stockmanager.scheduler.tick import SummaryJobs
from stockmanager.store import FileStore
from stockmanager.store.lock import Clock, now_pt

CONFIG_FILENAME = "config.yaml"
#: AS §2.5: secrets live outside the workspace so anything that can read the workspace (dashboard, Hermes agent)
#: never also gets the keys. Overridable with ``STOCKMANAGER_SECRETS`` (matching the architecture spec's env var).
DEFAULT_SECRETS_PATH = Path.home() / ".config" / "stockmanager" / "secrets.env"
_SECRET_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


class WorkspaceMissing(Exception):
    """The workspace directory does not exist; ``stockmanager init`` has not been run (AS §1.3)."""


class ConfigInvalid(Exception):
    """``config.yaml`` exists but fails Pydantic validation; the app refuses to start on bad config (AS §3.6)."""


def _read_yaml_mapping(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise ConfigInvalid(f"malformed {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigInvalid(f"{path} is not a YAML mapping")
    return data


def load_config(workspace: Path, config_path: Path | None = None) -> Config:
    """Validate ``config.yaml`` (or ``config_path``) if present; defaults otherwise. Raises ``ConfigInvalid``."""
    data = _read_yaml_mapping(config_path if config_path is not None else workspace / CONFIG_FILENAME)
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigInvalid(f"invalid config.yaml: {exc.errors()[0]['msg']}") from exc


def load_secrets(path: Path | None = None) -> None:
    """Load ``KEY=VALUE`` lines from the secrets file into ``os.environ``, never overriding a set variable.

    Mirrors AS §2.5's ``config.load_secrets()``. A missing file is not an error: secrets may also come from the
    real environment (CI, launchd ``EnvironmentVariables``). Expects ``DISCORD_BOT_TOKEN``, ``FINNHUB_API_KEY``
    and ``ANTHROPIC_API_KEY`` (the Anthropic SDK reads the last one itself).
    """
    secrets_path = path or Path(os.environ.get("STOCKMANAGER_SECRETS", str(DEFAULT_SECRETS_PATH)))
    if not secrets_path.exists():
        return
    for line in secrets_path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        m = _SECRET_LINE.match(line)
        if m:
            os.environ.setdefault(m.group(1), m.group(2))


def _session_close_fn(calendar: JsonCalendar) -> Callable[[date], datetime]:
    """Close instants for the price providers: early-close aware, regular 16:00 ET for dates the calendar rejects."""

    def close(day: date) -> datetime:
        try:
            return calendar.session_close(day)
        except ValueError:  # not a session / outside coverage: the engines fail closed on their own calendar calls
            return regular_close(day)

    return close


def _build_price_provider(name: str, clock: Clock, cache_dir: Path, calendar: JsonCalendar) -> PriceProvider:
    if name == "yahoo":
        return YfinancePriceProvider(clock, session_close=_session_close_fn(calendar), cache_dir=cache_dir)
    if name == "finnhub":
        return FinnhubPriceProvider(
            os.environ.get("FINNHUB_API_KEY", ""), clock, session_close=_session_close_fn(calendar)
        )
    raise ConfigInvalid(f"unknown price source: {name!r}")  # pragma: no cover - Config already restricts this


def append_mainchat_channel(channels: list[Channel], channel_id: int | None, *, clock: Clock = now_pt) -> None:
    """Append the Hermes mainchat sender when a bot token is set and the config supplies a channel id.

    The token is ``MAINCHAT_BOT_TOKEN``, falling back to ``DISCORD_BOT_TOKEN`` for single-bot deployments. The channel
    id is the configured Discord channel (``cfg.discord.channel_id``); only the token comes from the environment, and
    it is never logged. Left unwired otherwise, so alerts with a pending ``mainchat`` state stay pending (fail closed,
    AS §9).
    """
    token = os.environ.get("MAINCHAT_BOT_TOKEN") or os.environ.get("DISCORD_BOT_TOKEN") or None
    if token and channel_id:
        channels.append(HermesMainchatChannel(token, channel_id, clock=clock))  # type: ignore[arg-type]


@dataclass
class AppContext:
    """Every wired component a CLI command, the tick dispatcher, or the bot needs (AS §1.3 composition root)."""

    workspace: Path
    store: FileStore
    cfg: Config
    clock: Clock
    calendar: SessionCalendar
    symbol_verifier: SymbolVerifier
    price_primary: PriceProvider
    price_fallback: PriceProvider
    execution: ExecutionEngine
    news_config: NewsConfig
    news_fetchers: list[object]
    extractor: Extractor
    reasoner: Reasoner
    evaluation: EvaluationEngine
    reconfirm: ReconfirmEngine
    command_ctx: CommandContext
    notify_channels: list[Channel] = field(default_factory=list)
    discord_bot_token: str | None = None
    discord_channel_id: int | None = None
    summary_jobs: SummaryJobs | None = None
    stock_query: StockQuery | None = None

    def run_news_stage(self, cycle_id: str, portfolio: PortfolioDoc) -> NewsResult:
        """``NewsRunner`` (AS §7): run the whole news stage for one cycle against ``portfolio``."""
        return run_news_stage(
            cycle_id,
            portfolio,
            self.news_config,
            self.clock,
            self.news_fetchers,  # type: ignore[arg-type]
            self.extractor,
            store=self.store,
        )


def create_app(workspace_path: str | Path, config_path: str | Path | None = None, *, clock: Clock = now_pt) -> AppContext:
    """Wire the composition root from ``workspace_path`` (AS §1.3). No I/O side effects beyond reading config files.

    Raises ``WorkspaceMissing`` if the workspace does not exist (run ``stockmanager init`` first) and
    ``ConfigInvalid`` if ``config.yaml``/``config_path`` exists but fails validation.
    """
    workspace = Path(workspace_path)
    if not workspace.exists():
        raise WorkspaceMissing(f"workspace not found: {workspace}; run `stockmanager init` first")

    load_secrets()
    cfg = load_config(workspace, Path(config_path) if config_path is not None else None)
    use_catalogue(cfg.ops.notification_catalogue)
    store = FileStore(workspace, clock=clock)

    calendar = JsonCalendar.load()
    symbol_verifier = YahooSymbolVerifier()
    price_primary = _build_price_provider(cfg.price_source_primary, clock, workspace, calendar)
    price_fallback = _build_price_provider(cfg.price_source_fallback, clock, workspace, calendar)

    execution = ExecutionEngine(
        store, clock, calendar, price_primary, price_fallback, exec_delay_minutes=cfg.ops.exec_delay_minutes
    )

    news_config = NewsConfig(
        benchmark=cfg.benchmark,
        max_items_per_query=cfg.news.max_items_per_query,
        max_facts_per_ticker=cfg.news.max_facts_per_ticker,
    )
    news_fetchers: list[object] = [
        SearxngFetcher(os.environ.get("SEARXNG_URL", "http://localhost:8080")),
        FinnhubNewsFetcher(),
        YahooRssFetcher(),
    ]
    # The Extractor's call differs from the Reasoner's (no thinking, effort low, not streamed, its own retries), so
    # each gets its own Anthropic backend instance; swap either for another LLMBackend here.
    reasoner_backend = AnthropicLLMBackend(cfg.llm, cfg.proposals)
    extractor_backend = AnthropicLLMBackend(
        cfg.llm,
        cfg.proposals,
        thinking="disabled",
        effort="low",
        stream=False,
        max_retries=news_config.extractor.max_retries,
    )
    extractor = build_extractor(extractor_backend, news_config)
    reasoner = build_reasoner(reasoner_backend, cfg.llm, cfg.proposals)

    command_ctx = CommandContext(
        store=store, cfg=cfg, clock=clock, calendar=calendar, price_provider=price_primary, price_fallback=price_fallback
    )

    notify_channels: list[Channel] = []
    discord_bot_token = os.environ.get("DISCORD_BOT_TOKEN") or None
    discord_channel_id = int(cfg.discord.channel_id) if cfg.discord.channel_id else None
    if discord_bot_token and discord_channel_id:
        notify_channels.append(DiscordRESTChannel(discord_bot_token, discord_channel_id, clock=clock))  # type: ignore[arg-type]
    append_mainchat_channel(notify_channels, discord_channel_id, clock=clock)

    # AI summaries run on the local llama.cpp server through its OpenAI-compatible API, never on the Anthropic backend.
    # It needs no real key (the openai client just wants a non-empty one) and the Anthropic key is not involved.
    summary_backend = OpenAILLMBackend(cfg.summary.base_url, timeout=float(cfg.summary.timeout_s), max_retries=1)
    summary_gen = partial(generate_summary, config=cfg.summary, backend=summary_backend)
    news_lookup = make_news_lookup(news_fetchers, news_config)  # type: ignore[arg-type]
    summary_jobs = SummaryJobs(
        calendar=calendar,
        digest=lambda: run_digest(
            store,
            cfg,
            summary_gen,
            notify_channels,
            clock,
            price_provider=price_primary,
            price_fallback=price_fallback,
            news_lookup=news_lookup,
        ),
        price_check=lambda: run_price_check(
            store,
            cfg,
            price_primary,
            summary_gen,
            notify_channels,
            clock,
            price_fallback=price_fallback,
            news_lookup=news_lookup,
        ),
    )
    stock_query = StockQuery(command_ctx, calendar, news_lookup, summary_gen, search_symbols)

    app_ctx = AppContext(
        workspace=workspace,
        store=store,
        cfg=cfg,
        clock=clock,
        calendar=calendar,
        symbol_verifier=symbol_verifier,
        price_primary=price_primary,
        price_fallback=price_fallback,
        execution=execution,
        news_config=news_config,
        news_fetchers=news_fetchers,
        extractor=extractor,
        reasoner=reasoner,
        evaluation=None,  # type: ignore[arg-type]  # set just below; EvaluationEngine needs app_ctx.run_news_stage
        reconfirm=ReconfirmEngine(
            store, clock, calendar, symbol_verifier, price_primary, price_fallback, ThesisReview(store)
        ),
        command_ctx=command_ctx,
        notify_channels=notify_channels,
        discord_bot_token=discord_bot_token,
        discord_channel_id=discord_channel_id,
        summary_jobs=summary_jobs,
        stock_query=stock_query,
    )
    app_ctx.evaluation = EvaluationEngine(
        store,
        clock,
        calendar,
        symbol_verifier,
        price_primary,
        price_fallback,
        reasoner,
        app_ctx.run_news_stage,
        make_reasoner_input_builder(store),
    )
    return app_ctx


def init_workspace(workspace: Path, *, start_cash: str = "50000") -> None:
    """Create a fresh workspace (UX §2): directories, default ``config.yaml``, and a ``STAGED`` ``portfolio.json``.

    Secrets are never written here (AS §2.5: they live in ``~/.config/stockmanager/secrets.env``, outside the
    workspace). Idempotent: re-running on an already-initialized workspace leaves ``config.yaml`` and
    ``portfolio.json`` untouched.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    os.chmod(workspace, stat.S_IRWXU)
    for sub in ("proposals", "state", "run-notes", "dashboard", "logs", "alerts/pending", "extracted-facts"):
        (workspace / sub).mkdir(parents=True, exist_ok=True)

    config_path = workspace / CONFIG_FILENAME
    if not config_path.exists():
        cfg = Config.model_validate({"start_cash": start_cash})
        config_path.write_text(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
        os.chmod(config_path, stat.S_IRUSR | stat.S_IWUSR)

    store = FileStore(workspace)
    if not store.portfolio_exists():
        from decimal import Decimal

        store.save_portfolio(PortfolioDoc(start_cash=Decimal(start_cash), cash=Decimal(start_cash), status="STAGED"))
