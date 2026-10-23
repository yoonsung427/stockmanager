"""AI summaries: generator, digest, price check, stock query, scheduling and the bot fallback."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from stockmanager.adapters.calendar import JsonCalendar
from stockmanager.bot.bot import PortfolioBot
from stockmanager.commands import CommandContext
from stockmanager.config import BLOCKED_SET_PREFIXES, Config, DiscordConfig, SummaryConfig
from stockmanager.jobs.common import dump_last_close, load_last_close
from stockmanager.jobs.digest import run_digest
from stockmanager.jobs.price_check import run_price_check
from stockmanager.jobs.stock_query import StockQuery, extract_candidate
from stockmanager.llm.protocol import ChatResult, LLMError
from stockmanager.llm.summary import (
    NewsItem,
    PositionInfo,
    SummaryError,
    generate_summary,
    load_summary_prompt,
    render_user_prompt,
)
from stockmanager.notify.outbox import Alert
from stockmanager.scheduler.scheduler import list_due_jobs
from stockmanager.scheduler.state import SchedulerState
from stockmanager.sim.prices import ScriptedPriceProvider
from stockmanager.store import FileStore
from tests.support.books import FakePidChecker, FixedClock, make_portfolio, make_trade

PT = ZoneInfo("America/Los_Angeles")
FRI = date(2026, 10, 23)  # a regular session; EDT, so 09:30 ET = 06:30 PT and 16:00 ET = 13:00 PT


def at(h: int, m: int = 0, day: date = FRI) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=PT)


class FakeBackend:
    """``LLMBackend`` double: records calls, returns ``text`` or raises ``error``."""

    def __init__(self, text: str = "MSFT is up.", stop: str = "stop", error: Exception | None = None) -> None:
        self.text, self.stop, self.error = text, stop, error
        self.calls: list[dict[str, Any]] = []

    def chat_completion(self, **kwargs: Any) -> ChatResult:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return ChatResult(self.text, self.stop, "fake")


def fake_gen(text: str = "summary text") -> Any:
    calls: list[tuple[str, PositionInfo, int]] = []

    def gen(ticker: str, news: Any, info: PositionInfo, session: date) -> str:
        calls.append((ticker, info, len(news)))
        return text

    gen.calls = calls  # type: ignore[attr-defined]
    return gen


def failing_gen(*_: Any) -> str:
    raise SummaryError("down")


def news_for(*tickers: str) -> Any:
    return lambda ticker, session: [NewsItem(f"{ticker} headline")] if ticker in tickers else []


class Channel:
    name = "discord"

    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send(self, rendered: Any, *, nonce: str) -> Any:
        from stockmanager.notify.deliver import Receipt

        self.sent.append(rendered)
        return Receipt("discord", nonce, FixedClock()())


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(at(13, 30))  # 16:30 ET: the close has settled


@pytest.fixture
def store(tmp_path: Path, clock: FixedClock) -> FileStore:
    s = FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)
    for sub in ("state", "alerts/pending"):
        (s.root / sub).mkdir(parents=True)
    s.save_portfolio(make_portfolio([make_trade(1, ticker="MSFT", shares="10", price="400"), make_trade(2, ticker="AAPL")]))
    return s


def provider(clock: FixedClock, **prices: str) -> ScriptedPriceProvider:
    return ScriptedPriceProvider("yahoo", {t: Decimal(p) for t, p in prices.items()}, clock)


def pending(store: FileStore) -> list[Alert]:
    return [Alert.from_json(p.read_text()) for p in sorted((store.root / "alerts" / "pending").glob("*.json"))]


# -- config --------------------------------------------------------------------------------------------------------


def test_summary_config_defaults_point_at_local_lm_studio() -> None:
    cfg = SummaryConfig()
    assert cfg.base_url == "http://localhost:1234/v1"
    assert (cfg.model, cfg.max_tokens, cfg.temperature, cfg.timeout_s, cfg.price_threshold_pct) == ("google/gemma-4-12b", 4000, 0.3, 60, 5.0)
    assert Config().summary == cfg
    assert "summary." in BLOCKED_SET_PREFIXES


# -- generator -----------------------------------------------------------------------------------------------------


def test_prompt_is_hash_verified_and_renders_decimal_figures() -> None:
    prompt = load_summary_prompt()
    info = PositionInfo(Decimal("6.96"), Decimal(2800), Decimal("432.10"), FRI, Decimal("-5.5"))
    user = render_user_prompt(prompt, "MSFT", [NewsItem("Beat <b>big</b>", "Rev up", "reuters.com", FRI)], info, FRI)
    assert "Shares held: 6.96" in user and "Market value: 3,007.42" in user and "(+7.41%)" in user
    assert "Move vs previous close: -5.50%" in user
    assert "Beat big" in user and "<b>" not in user


def test_generate_summary_calls_backend_with_config_and_returns_text() -> None:
    backend = FakeBackend("  MSFT rose 2%.  ")
    out = generate_summary("MSFT", [], PositionInfo(Decimal(1), Decimal(100)), FRI, SummaryConfig(max_tokens=123), backend)
    assert out == "MSFT rose 2%."
    call = backend.calls[0]
    assert (call["model"], call["max_tokens"], call["timeout"]) == ("google/gemma-4-12b", 123, 60.0)
    assert call.get("json_schema") is None
    assert call["system"].startswith("You are a portfolio analyst")
    assert "(no recent news found)" in call["user"] and "unavailable" in call["user"]


@pytest.mark.parametrize(
    "raw", ["<think>hmm</think>\nAnswer.", "<THINK>\nhmm\nmore</THINK>Answer.", "hmm\n</think>\nAnswer."]
)
def test_reasoning_blocks_are_stripped(raw: str) -> None:
    out = generate_summary("X", [], PositionInfo(Decimal(1), Decimal(1)), FRI, SummaryConfig(), FakeBackend(raw))
    assert out == "Answer."


@pytest.mark.parametrize(
    "backend",
    [FakeBackend(error=LLMError("boom")), FakeBackend(""), FakeBackend("x", "refusal"), FakeBackend("<think>a</think>")],
)
def test_generate_summary_raises_summary_error_without_usable_text(backend: FakeBackend) -> None:
    with pytest.raises(SummaryError):
        generate_summary("X", [], PositionInfo(Decimal(1), Decimal(1)), FRI, SummaryConfig(), backend)


def test_no_ollama_port_anywhere_in_source() -> None:
    root = Path(__file__).resolve().parents[2] / "src"
    assert not [p for p in root.rglob("*") if p.is_file() and p.suffix in {".py", ".md", ".yaml", ".j2"} and "11434" in p.read_text()]


# -- last_close.json -----------------------------------------------------------------------------------------------


def test_last_close_roundtrip_is_decimal_exact_and_tolerant(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    text = dump_last_close({"MSFT": Decimal("432.10"), "AAPL": Decimal("234.5")})
    assert json.loads(text) == {"AAPL": 234.5, "MSFT": 432.1}  # plain JSON numbers, as agreed
    (tmp_path / "state" / "last_close.json").write_text(text)
    assert load_last_close(tmp_path) == {"MSFT": Decimal("432.1"), "AAPL": Decimal("234.5")}
    (tmp_path / "state" / "last_close.json").write_text('{"A": "x", "B": -1, "C": 5, "D": "7.25"}')
    assert load_last_close(tmp_path) == {"C": Decimal(5), "D": Decimal("7.25")}
    (tmp_path / "state" / "last_close.json").write_text("not json")
    assert load_last_close(tmp_path) == {}


# -- digest --------------------------------------------------------------------------------------------------------


def run_digest_with(store: FileStore, clock: FixedClock, gen: Any, news: Any, ch: Any = (), **prices: str) -> list[str]:
    return run_digest(
        store, Config(), gen, ch, clock, price_provider=provider(clock, **prices), news_lookup=news
    )


def test_digest_summarises_only_notable_holdings_and_rolls_last_close(store: FileStore, clock: FixedClock) -> None:
    (store.root / "state" / "last_close.json").write_text(dump_last_close({"MSFT": Decimal(400), "AAPL": Decimal(100)}))
    gen = fake_gen("It moved.")
    # MSFT +2% (notable); AAPL +0.5% with no news (quiet)
    out = run_digest_with(store, clock, gen, news_for(), MSFT="408", AAPL="100.5")
    assert out == ["**MSFT** — It moved."]
    assert [c[0] for c in gen.calls] == ["MSFT"]
    assert gen.calls[0][1].day_change_pct == Decimal("2.0000")
    (alert,) = pending(store)
    assert alert.kind == "daily_digest" and "**MSFT** — It moved." in alert.body
    assert load_last_close(store.root) == {"MSFT": Decimal(408), "AAPL": Decimal("100.5")}


def test_digest_counts_news_as_notable_and_sends_one_alert(store: FileStore, clock: FixedClock) -> None:
    ch = Channel()
    out = run_digest_with(store, clock, fake_gen("S"), news_for("AAPL"), [ch], MSFT="400", AAPL="100")
    assert out == ["**AAPL** — S"]  # no previous closes on file, so news is the only trigger
    assert len(pending(store)) + len(ch.sent) >= 1


def test_digest_quiet_day_posts_nothing_but_still_rolls_closes(store: FileStore, clock: FixedClock) -> None:
    assert run_digest_with(store, clock, fake_gen(), news_for(), MSFT="400", AAPL="100") == []
    assert pending(store) == []
    assert set(load_last_close(store.root)) == {"MSFT", "AAPL"}


def test_digest_price_outage_fails_closed_on_state_but_judges_on_news(store: FileStore, clock: FixedClock) -> None:
    before = dump_last_close({"MSFT": Decimal(1)})
    (store.root / "state" / "last_close.json").write_text(before)
    out = run_digest_with(store, clock, fake_gen("S"), news_for("MSFT"))  # nothing scripted: every price is missing
    assert out == ["**MSFT** — S"]
    assert (store.root / "state" / "last_close.json").read_text() == before


def test_digest_raises_when_every_summary_fails_so_the_tick_retries(store: FileStore, clock: FixedClock) -> None:
    with pytest.raises(SummaryError):
        run_digest_with(store, clock, failing_gen, news_for("MSFT"), MSFT="400", AAPL="100")
    assert pending(store) == [] and not (store.root / "state" / "last_close.json").exists()


def test_digest_disabled_still_rolls_closes_without_summaries(store: FileStore, clock: FixedClock) -> None:
    cfg = Config(summary=SummaryConfig(digest_at_close=False))
    gen = fake_gen()
    out = run_digest(store, cfg, gen, [], clock, price_provider=provider(clock, MSFT="1", AAPL="2"), news_lookup=news_for("MSFT"))
    assert out == [] and gen.calls == [] and set(load_last_close(store.root)) == {"MSFT", "AAPL"}


# -- price check ---------------------------------------------------------------------------------------------------


def check(store: FileStore, clock: FixedClock, gen: Any = None, cfg: Config | None = None, **prices: str) -> list[str]:
    return run_price_check(
        store, cfg or Config(), provider(clock, **prices), gen or fake_gen("Gap."), [], clock, news_lookup=news_for("MSFT")
    )


def test_price_check_alerts_only_beyond_threshold(store: FileStore, clock: FixedClock) -> None:
    (store.root / "state" / "last_close.json").write_text(dump_last_close({"MSFT": Decimal(400), "AAPL": Decimal(100)}))
    gen = fake_gen("Gap.")
    out = check(store, clock, gen, MSFT="380", AAPL="103")  # -5.0% (at threshold) and +3%
    assert out == ["Gap."]
    (alert,) = pending(store)
    assert (alert.kind, alert.severity, alert.title) == ("summary_alert", "alert", "Summary: MSFT")
    assert alert.dedupe_key == "price_move:MSFT:2026-10-23" and gen.calls[0][1].day_change_pct == Decimal("-5.0000")
    assert gen.calls[0][2] == 1  # the news was fetched and passed in


def test_price_check_is_idempotent_per_ticker_per_day(store: FileStore, clock: FixedClock) -> None:
    (store.root / "state" / "last_close.json").write_text(dump_last_close({"MSFT": Decimal(400)}))
    check(store, clock, MSFT="300", AAPL="100")
    assert check(store, clock, MSFT="300", AAPL="100") == []
    assert len(pending(store)) == 1


def test_price_check_sends_numbers_when_the_llm_is_down(store: FileStore, clock: FixedClock) -> None:
    (store.root / "state" / "last_close.json").write_text(dump_last_close({"MSFT": Decimal(400)}))
    (text,) = check(store, clock, failing_gen, MSFT="440", AAPL="100")
    assert "+10.00%" in text and "400.00 -> 440.00" in text


def test_price_check_without_baseline_or_when_off_does_nothing(store: FileStore, clock: FixedClock) -> None:
    assert check(store, clock, MSFT="1", AAPL="1") == []
    (store.root / "state" / "last_close.json").write_text(dump_last_close({"MSFT": Decimal(400)}))
    off = Config(summary=SummaryConfig(alert_on_threshold=False))
    assert check(store, clock, cfg=off, MSFT="1", AAPL="1") == []
    assert pending(store) == []


def test_price_check_fails_closed_when_a_price_is_missing(store: FileStore, clock: FixedClock) -> None:
    from stockmanager.pricing.errors import ValuationUnavailable

    (store.root / "state" / "last_close.json").write_text(dump_last_close({"MSFT": Decimal(400), "AAPL": Decimal(100)}))
    with pytest.raises(ValuationUnavailable):
        check(store, clock, MSFT="300")  # AAPL unscripted: nothing is alerted on a partial table
    assert pending(store) == []


# -- scheduling ----------------------------------------------------------------------------------------------------


CAL = JsonCalendar.load()


def due(now: datetime, state: SchedulerState | None = None, cfg: Config | None = None) -> list[str]:
    jobs = list_due_jobs(now, cfg or Config(), "LIVE", [], state or SchedulerState(), calendar=CAL)
    return [j.kind for j in jobs if j.kind in ("digest", "price_check")]


def test_price_check_is_due_from_0945_et_until_the_close() -> None:
    assert due(at(6, 44)) == [] and due(at(6, 45)) == ["price_check"] and due(at(12, 59)) == ["price_check"]
    assert "price_check" not in due(at(13, 0))


def test_digest_is_due_after_the_close_settles_for_a_bounded_window() -> None:
    assert "digest" not in due(at(13, 19)) and "digest" in due(at(13, 20)) and "digest" in due(at(16, 19))
    assert "digest" not in due(at(16, 21))


def test_summary_jobs_run_once_per_day_trading_days_only_and_honour_config() -> None:
    done = SchedulerState().with_price_check_done(FRI).with_digest_done(FRI)
    assert due(at(6, 50), done) == [] and due(at(13, 30), done) == []
    assert due(at(6, 50, date(2026, 10, 24))) == []  # Saturday
    assert due(at(6, 50), cfg=Config(summary=SummaryConfig(enabled=False))) == []
    assert due(at(13, 30), cfg=Config(summary=SummaryConfig(digest_at_close=False, alert_on_threshold=False))) == []
    assert due(at(13, 30), cfg=Config(summary=SummaryConfig(digest_at_close=False))) == ["digest"]  # still rolls closes


def test_early_close_moves_the_digest_and_no_calendar_means_no_summary_jobs() -> None:
    early = date(2026, 11, 27)  # day after Thanksgiving: 13:00 ET close = 10:00 PT
    assert "digest" in due(at(10, 30, early)) and "digest" not in due(at(13, 30, early))
    no_calendar = list_due_jobs(at(13, 30), Config(), "LIVE", [], SchedulerState())
    assert not [j for j in no_calendar if j.kind in ("digest", "price_check")]


def test_scheduler_state_roundtrips_the_new_fields_and_reads_old_files() -> None:
    s = SchedulerState().with_digest_done(FRI).with_price_check_done(FRI)
    assert SchedulerState.from_json(s.to_json()) == s
    assert SchedulerState.from_json('{"anchor_date": null}').digest_days == ()


def test_tick_runs_due_summary_jobs_once_and_retries_a_failed_one(store: FileStore, clock: FixedClock) -> None:
    from stockmanager.scheduler import state as scheduler_state
    from stockmanager.scheduler.tick import SummaryJobs, run_locked

    clock.now = at(13, 30)  # digest due, price check over
    ran: list[str] = []
    fail = [True]

    def digest() -> list[str]:
        ran.append("digest")
        if fail[0]:
            raise RuntimeError("price outage")
        return []

    def price_check() -> list[str]:
        ran.append("price_check")
        return []

    jobs = SummaryJobs(CAL, digest, price_check)
    assert run_locked(clock, store, Config(), summary=jobs) == 0  # a failing job never fails the tick
    assert scheduler_state.load(store.root).digest_days == ()
    fail[0] = False
    assert run_locked(clock, store, Config(), summary=jobs) == 0
    assert run_locked(clock, store, Config(), summary=jobs) == 0
    assert ran == ["digest", "digest"]  # done for the day after the retry succeeded
    assert scheduler_state.load(store.root).digest_days == (FRI,)


# -- stock query ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("what's going on with MSFT?", "MSFT"),
        ("What’s going on with Microsoft", "Microsoft"),
        ("whats going on with msft", "msft"),
        ("MSFT news", "MSFT"),
        ("Microsoft news?", "Microsoft"),
        ("check MSFT", "MSFT"),
        ("CHECK  msft!", "msft"),
        ("any good news today", None),
        ("hello there", None),
        ("check " + "x" * 80, None),
    ],
)
def test_extract_candidate(text: str, expected: str | None) -> None:
    assert extract_candidate(text) == expected


@pytest.fixture
def query_ctx(store: FileStore, clock: FixedClock) -> CommandContext:
    cfg = Config(discord=DiscordConfig(allowed_user_ids=["1"], channel_id="5"))
    return CommandContext(
        store=store, cfg=cfg, clock=clock, calendar=CAL, price_provider=provider(clock, MSFT="440", AAPL="100")
    )


def make_query(ctx: CommandContext, gen: Any = None, aliases: Any = None) -> StockQuery:
    return StockQuery(ctx, CAL, news_for("MSFT"), gen or fake_gen("All quiet."), aliases or (lambda n: []))


def test_stock_query_replies_for_an_allowed_user_with_position_and_news(query_ctx: CommandContext) -> None:
    gen = fake_gen("All quiet.")
    q = make_query(query_ctx, gen)
    assert q("check msft", "1") == "**MSFT** — All quiet."
    ticker, info, n_news = gen.calls[0]
    assert (ticker, info.shares, info.cost_basis, info.price, n_news) == ("MSFT", Decimal(10), Decimal(4000), Decimal(440), 1)
    assert info.price_date == FRI and info.pnl == Decimal("400.0000")


def test_stock_query_resolves_company_names_through_the_alias_lookup(query_ctx: CommandContext) -> None:
    seen: list[str] = []

    def aliases(name: str) -> list[str]:
        seen.append(name)
        return ["MSFT34", "MSFT"]

    q = make_query(query_ctx, aliases=aliases)
    assert q("what's going on with Microsoft?", "1") == "**MSFT** — All quiet."
    q("Microsoft news", "1")
    assert seen == ["Microsoft"]  # cached


def test_stock_query_is_private_and_quiet_about_non_matches(query_ctx: CommandContext) -> None:
    q = make_query(query_ctx)
    assert q("check MSFT", "999") is None  # not on the allow-list: nothing about positions leaks
    assert q("check ZZZZ", "1") == "⚠️ ZZZZ is not in the portfolio."
    assert q("good news", "1") is None and q("check my balance now", "1") is None
    off = query_ctx.cfg.model_copy(update={"summary": SummaryConfig(enabled=False)})
    assert make_query(CommandContext(**{**query_ctx.__dict__, "cfg": off}))("check MSFT", "1") is None


def test_stock_query_survives_llm_and_price_failures(query_ctx: CommandContext) -> None:
    assert "Couldn't generate" in (make_query(query_ctx, failing_gen)("check MSFT", "1") or "")
    gen = fake_gen()
    query_ctx.price_provider.fail("MSFT")  # type: ignore[attr-defined]
    assert make_query(query_ctx, gen)("check MSFT", "1") is not None
    assert gen.calls[0][1].price is None  # fail closed on the price, never guess it


# -- bot fallback --------------------------------------------------------------------------------------------------


def make_message(content: str, author_id: int = 1) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 5
    message = MagicMock(spec=discord.Message)
    message.channel = channel
    message.author = MagicMock()
    message.author.bot = False
    message.author.id = author_id
    message.id = 100
    message.content = content
    message.created_at = at(13, 30)
    message.reply = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_bot_tries_stock_query_only_for_unrecognised_text(query_ctx: CommandContext, store: FileStore) -> None:
    asked: list[tuple[str, str]] = []

    def stock(text: str, user: str) -> str | None:
        asked.append((text, user))
        return "**MSFT** — fine" if "msft" in text else None

    bot = PortfolioBot(store, 5, query_ctx, [], stock_query=stock)
    m1, m2, m3 = make_message("STATUS"), make_message("what's going on with msft?"), make_message("blah")
    for m in (m1, m2, m3):
        await bot.on_message(m)
    assert m1.reply.call_args.args[0].startswith("✅")
    assert m2.reply.call_args.args[0] == "**MSFT** — fine"
    assert m3.reply.call_args.args[0] == "❌ Unrecognized command"
    assert asked == [("what's going on with msft?", "1"), ("blah", "1")]  # real commands never reach it
    # a stock answer is not a rejection: only the "blah" message raised a rejected_control_message alert
    assert [a.kind for a in pending(store)] == ["rejected_control_message"]
