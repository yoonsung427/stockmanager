"""v2 H6: tick output through the summary layer is delivered as catalogue-shaped messages; silent ticks send nothing."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from stockmanager.config import Config
from stockmanager.config.model import NotificationTemplate, OpsConfig
from stockmanager.jobs.common import dump_last_close
from stockmanager.jobs.digest import run_digest
from stockmanager.jobs.price_check import run_price_check
from stockmanager.notify.deliver import SEVERITY_COLORS, Rendered
from stockmanager.notify.templates import DEFAULTS, use_catalogue
from stockmanager.scheduler.tick import SummaryJobs, run_locked
from stockmanager.sim.channels import RecordingChannel
from stockmanager.sim.clock import FakeClock
from stockmanager.store import FileStore
from tests.support.books import FakePidChecker, FixedClock, make_portfolio, make_trade
from tests.unit.test_summary import CAL, at, fake_gen, news_for, pending, provider

PRICE_CHECK_TIME = at(6, 50)  # 09:50 ET: the morning price check is due
DIGEST_TIME = at(13, 30)  # 16:30 ET: the close has settled


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(DIGEST_TIME)


@pytest.fixture
def store(tmp_path: Path, clock: FixedClock) -> FileStore:
    s = FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)
    for sub in ("state", "alerts/pending"):
        (s.root / sub).mkdir(parents=True)
    s.save_portfolio(make_portfolio([make_trade(1, ticker="MSFT", shares="10", price="400"), make_trade(2, ticker="AAPL")]))
    (s.root / "state" / "last_close.json").write_text(dump_last_close({"MSFT": Decimal(400), "AAPL": Decimal(100)}))
    return s


@pytest.fixture
def discord_channel() -> RecordingChannel:
    return RecordingChannel("discord", FakeClock(DIGEST_TIME))


@pytest.fixture(autouse=True)
def default_catalogue() -> Iterator[None]:
    use_catalogue([])
    yield
    use_catalogue([])


def summary_jobs(
    store: FileStore, clock: FixedClock, channel: RecordingChannel, gen: Any, news: Any = None, **prices: str
) -> SummaryJobs:
    """The two jobs bound as the composition root binds them, but with a summary-layer fake and scripted prices."""
    cfg, prov = Config(), provider(clock, **prices)
    lookup = news or news_for()
    return SummaryJobs(
        CAL,
        lambda: run_digest(store, cfg, gen, [channel], clock, price_provider=prov, news_lookup=lookup),
        lambda: run_price_check(store, cfg, prov, gen, [channel], clock, news_lookup=lookup),
    )


def embed_of(rendered: Rendered) -> dict[str, Any]:
    assert rendered.embed is not None
    return rendered.embed


def test_digest_tick_delivers_a_catalogue_shaped_info_message(
    store: FileStore, clock: FixedClock, discord_channel: RecordingChannel
) -> None:
    jobs = summary_jobs(store, clock, discord_channel, fake_gen("It moved."), MSFT="408", AAPL="100.5")  # MSFT +2%
    assert run_locked(clock, store, Config(), channels=[discord_channel], summary=jobs) == 0
    [(_, rendered)] = discord_channel.sent
    severity, title, _ = DEFAULTS["daily_digest"]
    embed = embed_of(rendered)
    assert (rendered.text, embed["title"]) == (title, "Daily digest")
    assert embed["color"] == SEVERITY_COLORS[severity] == SEVERITY_COLORS["info"]
    assert "**MSFT** — It moved." in embed["description"] and "AAPL" not in embed["description"]
    assert embed["fields"] == [{"name": "holdings", "value": "1", "inline": True}]
    assert rendered.components is None  # narrative, not a decision: no buttons


def test_price_move_tick_delivers_a_catalogue_shaped_alert_class_message(
    store: FileStore, clock: FixedClock, discord_channel: RecordingChannel
) -> None:
    clock.now = PRICE_CHECK_TIME
    jobs = summary_jobs(store, clock, discord_channel, fake_gen("Gapped down."), MSFT="380", AAPL="100")  # MSFT -5%
    assert run_locked(clock, store, Config(), channels=[discord_channel], summary=jobs) == 0
    [(_, rendered)] = discord_channel.sent
    embed = embed_of(rendered)
    assert embed["title"] == "Summary: MSFT" and embed["description"] == "Gapped down."
    assert embed["color"] == SEVERITY_COLORS["alert"]  # a price move is an alert-class narrative message
    assert embed["fields"] == [{"name": "ticker", "value": "MSFT", "inline": True}]
    [stored] = pending(store)  # still queued only for the main chat, which this test does not wire
    assert (stored.channels["discord"].state, stored.channels["mainchat"].state) == ("sent", "pending")


def test_owner_catalogue_override_shapes_the_narrative_message(
    store: FileStore, clock: FixedClock, discord_channel: RecordingChannel
) -> None:
    override = NotificationTemplate(kind="daily_digest", title="EOD wrap", body="{summary}", severity="positive")
    cfg = Config(ops=OpsConfig(notification_catalogue=[override]))  # the tick installs the catalogue from its config
    jobs = summary_jobs(store, clock, discord_channel, fake_gen("Up."), MSFT="408", AAPL="100")
    assert run_locked(clock, store, cfg, channels=[discord_channel], summary=jobs) == 0
    embed = embed_of(discord_channel.sent[0][1])
    assert (embed["title"], embed["color"]) == ("EOD wrap", SEVERITY_COLORS["positive"])


def test_silent_digest_tick_sends_and_queues_nothing(
    store: FileStore, clock: FixedClock, discord_channel: RecordingChannel
) -> None:
    gen = fake_gen("never used")
    jobs = summary_jobs(store, clock, discord_channel, gen, MSFT="400.5", AAPL="100")  # +0.1%, no news: nothing notable
    assert run_locked(clock, store, Config(), channels=[discord_channel], summary=jobs) == 0
    assert discord_channel.sent == [] and pending(store) == [] and gen.calls == []  # not even a summary request


def test_silent_price_check_tick_sends_and_queues_nothing(
    store: FileStore, clock: FixedClock, discord_channel: RecordingChannel
) -> None:
    clock.now = PRICE_CHECK_TIME
    gen = fake_gen("never used")
    jobs = summary_jobs(store, clock, discord_channel, gen, MSFT="402", AAPL="99")  # ±2%: inside the 5% threshold
    assert run_locked(clock, store, Config(), channels=[discord_channel], summary=jobs) == 0
    assert discord_channel.sent == [] and pending(store) == [] and gen.calls == []


def test_repeat_tick_after_a_delivered_digest_stays_silent(
    store: FileStore, clock: FixedClock, discord_channel: RecordingChannel
) -> None:
    jobs = summary_jobs(store, clock, discord_channel, fake_gen("Up."), MSFT="408", AAPL="100")
    assert run_locked(clock, store, Config(), channels=[discord_channel], summary=jobs) == 0
    assert len(discord_channel.sent) == 1
    clock.now = datetime(2026, 10, 23, 13, 35, tzinfo=clock.now.tzinfo)
    assert run_locked(clock, store, Config(), channels=[discord_channel], summary=jobs) == 0
    assert len(discord_channel.sent) == 1  # the job is done for the day: the next tick sends nothing
