"""Notify: outbox, delivery, templates, and rendering (AS §9.2)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pytest

from stockmanager.notify import deliver as deliver_mod
from stockmanager.notify import templates
from stockmanager.notify.deliver import Permanent, Receipt, Rendered, Transient, deliver_pending, render
from stockmanager.notify.outbox import Alert, NeedsActionError, enqueue
from stockmanager.store import FileStore
from tests.support.books import FakePidChecker, FixedClock

PT = ZoneInfo("America/Los_Angeles")

ALL_TEMPLATES: list[Callable[[], Alert]] = [
    lambda: templates.evaluation_complete("eval-1", datetime(2026, 10, 12, tzinfo=PT).date()),
    lambda: templates.veto_window_open("P-2026-10-12-01", 4),
    lambda: templates.trade_executed("P-2026-10-12-01-buy1", "BUY", "AAPL", Decimal(5), Decimal(1000)),
    lambda: templates.risk_alert("drawdown", datetime(2026, 10, 12, tzinfo=PT).date(), "drawdown exceeded"),
    lambda: templates.earnings_approaching("AAPL", datetime(2026, 10, 20, tzinfo=PT).date()),
    lambda: templates.system_pause_resume("pause"),
    lambda: templates.system_pause_resume("resume"),
    lambda: templates.proposal_expired("P-2026-10-12-01", "no reconfirmation"),
    lambda: templates.reconcile_divergence("cash mismatch"),
    lambda: templates.rejected_control_message("discord:1"),
    lambda: templates.price_source_failure("evaluation", ["AAPL"], True),
    lambda: templates.price_source_failure("evaluation", ["AAPL"], False),
    lambda: templates.run_skipped_busy("evaluation", "2026-10-12T09:00"),
    lambda: templates.system_ready(),
    lambda: templates.config_error("deadbeef"),
    lambda: templates.news_unavailable(datetime(2026, 10, 12, tzinfo=PT).date()),
    lambda: templates.bot_offline(),
    lambda: templates.scheduler_stalled(),
    lambda: templates.clock_backwards(),
    lambda: templates.calendar_coverage(datetime(2026, 10, 12, tzinfo=PT).date()),
]


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def store(tmp_path: Path, clock: FixedClock) -> FileStore:
    return FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)


class FakeChannel:
    """A ``Channel`` double: queue up outcomes per call; records every ``send``."""

    def __init__(self, name: Literal["discord", "mainchat"], outcomes: list[Receipt | Exception]) -> None:
        self.name = name
        self._outcomes = list(outcomes)
        self.sent: list[tuple[Rendered, str]] = []

    def send(self, rendered: Rendered, *, nonce: str) -> Receipt:
        self.sent.append((rendered, nonce))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# -- templates --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("factory", ALL_TEMPLATES)
def test_needs_action_templates_carry_a_literal_command(factory: Callable[[], Alert]) -> None:
    alert = factory()
    if alert.needs_action:
        assert alert.commands, f"{alert.kind} needs_action but has no commands (TS AC-04)"


def test_every_template_has_a_dedupe_key_or_is_explicitly_none() -> None:
    for factory in ALL_TEMPLATES:
        alert = factory()
        assert alert.dedupe_key is None or isinstance(alert.dedupe_key, str)


def test_trade_executed_is_positive_severity() -> None:
    assert templates.trade_executed("t1", "BUY", "AAPL", Decimal(1), Decimal(100)).severity == "positive"


def test_risk_alert_is_alert_severity_with_pause_command() -> None:
    alert = templates.risk_alert("drawdown", datetime(2026, 10, 12, tzinfo=PT).date(), "detail")
    assert alert.severity == "alert"
    assert "PAUSE" in alert.commands


def test_price_source_failure_severity_depends_on_fallback() -> None:
    assert templates.price_source_failure("job", ["AAPL"], True).severity == "info"
    assert templates.price_source_failure("job", ["AAPL"], False).severity == "alert"


# -- outbox: enqueue -------------------------------------------------------------------------------------------


def test_enqueue_writes_pending_file_with_stamped_fields(store: FileStore, clock: FixedClock) -> None:
    alert = templates.system_ready()
    enqueue(alert, store)
    assert alert.alert_id
    assert alert.occurred_at == clock.now
    assert alert.channels
    files = list((store.root / "alerts" / "pending").glob("*.json"))
    assert len(files) == 1
    reloaded = Alert.from_json(files[0].read_text())
    assert reloaded.kind == "system_ready"


def test_enqueue_rejects_needs_action_without_commands(store: FileStore) -> None:
    alert = Alert(
        kind="broken",
        severity="alert",
        title="t",
        body="b",
        fields={},
        commands=[],
        needs_action=True,
        dedupe_key=None,
        binds={},
    )
    with pytest.raises(NeedsActionError):
        enqueue(alert, store)


def test_enqueue_creation_order_is_filename_order(store: FileStore, clock: FixedClock) -> None:
    enqueue(templates.system_ready(), store)
    clock.advance(seconds=1)
    enqueue(templates.bot_offline(), store)
    files = sorted((store.root / "alerts" / "pending").glob("*.json"))
    first = Alert.from_json(files[0].read_text())
    second = Alert.from_json(files[1].read_text())
    assert first.kind == "system_ready"
    assert second.kind == "bot_offline"


# -- delivery ---------------------------------------------------------------------------------------------------


def test_deliver_pending_sends_to_every_channel_and_moves_to_sent(store: FileStore, clock: FixedClock) -> None:
    enqueue(templates.system_ready(), store)
    discord_ch = FakeChannel("discord", [Receipt("discord", "1", clock.now)])
    mainchat_ch = FakeChannel("mainchat", [Receipt("mainchat", None, clock.now)])
    delivered = deliver_pending(store, [discord_ch, mainchat_ch], clock)
    assert len(delivered) == 2
    assert not list((store.root / "alerts" / "pending").glob("*.json"))
    assert len(list((store.root / "alerts" / "sent").glob("*.json"))) == 1
    assert len(discord_ch.sent) == 1 and len(mainchat_ch.sent) == 1


def test_deliver_pending_backs_off_on_transient_without_blocking_other_channels(store: FileStore, clock: FixedClock) -> None:
    enqueue(templates.system_ready(), store)
    discord_ch = FakeChannel("discord", [Transient("network blip")])
    mainchat_ch = FakeChannel("mainchat", [Receipt("mainchat", None, clock.now)])
    delivered = deliver_pending(store, [discord_ch, mainchat_ch], clock)
    assert delivered == [next(iter((store.root / "alerts" / "pending").glob("*.json"))).stem]
    pending = list((store.root / "alerts" / "pending").glob("*.json"))
    assert len(pending) == 1
    alert = Alert.from_json(pending[0].read_text())
    assert alert.channels["discord"].state == "pending"
    assert alert.channels["discord"].attempts == 1
    assert alert.channels["discord"].next_attempt_at is not None
    # mainchat is independent of discord's backoff and still gets sent
    assert alert.channels["mainchat"].state == "sent"
    assert len(mainchat_ch.sent) == 1


def test_deliver_pending_retries_after_backoff_elapses(store: FileStore, clock: FixedClock) -> None:
    enqueue(templates.system_ready(), store)
    discord_ch = FakeChannel("discord", [Transient("blip"), Receipt("discord", "1", clock.now)])
    mainchat_ch = FakeChannel("mainchat", [Receipt("mainchat", None, clock.now)])
    deliver_pending(store, [discord_ch, mainchat_ch], clock)
    clock.advance(seconds=4)
    assert deliver_pending(store, [discord_ch, mainchat_ch], clock) == []  # still within backoff
    clock.advance(seconds=2)
    delivered = deliver_pending(store, [discord_ch, mainchat_ch], clock)
    assert len(delivered) == 1
    assert len(list((store.root / "alerts" / "sent").glob("*.json"))) == 1


def test_deliver_pending_dead_letters_after_three_permanent_failures(store: FileStore, clock: FixedClock) -> None:
    enqueue(templates.system_ready(), store)
    discord_ch = FakeChannel("discord", [Permanent("401")] * 3)
    mainchat_ch = FakeChannel("mainchat", [Receipt("mainchat", None, clock.now)] * 4)
    for _ in range(3):
        deliver_pending(store, [discord_ch, mainchat_ch], clock)
    dead = list((store.root / "alerts" / "dead").glob("*.json"))
    assert len(dead) == 1
    # the dead letter triggered a mainchat notice
    notices = [Alert.from_json(p.read_text()) for p in (store.root / "alerts" / "pending").glob("*.json")]
    assert any(n.kind == "delivery_misconfigured" for n in notices)


def test_deliver_pending_honours_retry_after(store: FileStore, clock: FixedClock) -> None:
    enqueue(templates.system_ready(), store)
    discord_ch = FakeChannel("discord", [Transient("rate limited", retry_after=42.0), Receipt("discord", "1", clock.now)])
    mainchat_ch = FakeChannel("mainchat", [Receipt("mainchat", None, clock.now)])
    deliver_pending(store, [discord_ch, mainchat_ch], clock)
    pending = Alert.from_json(next((store.root / "alerts" / "pending").glob("*.json")).read_text())
    assert pending.channels["discord"].next_attempt_at == clock.now + timedelta(seconds=42.0)


# -- rendering --------------------------------------------------------------------------------------------------


def test_render_discord_embed_has_color_fields_and_command_block(clock: FixedClock) -> None:
    alert = templates.risk_alert("drawdown", clock.now.date(), "drawdown exceeded 5%")
    alert.occurred_at = clock.now
    rendered = render(alert, "discord", clock.now)
    assert rendered.embed is not None
    assert rendered.embed["color"] == deliver_mod.SEVERITY_COLORS["alert"]
    assert rendered.embed["timestamp"] == clock.now.isoformat()
    assert any("PAUSE" in f["value"] for f in rendered.embed["fields"])


def test_render_mainchat_is_markdown_text(clock: FixedClock) -> None:
    alert = templates.trade_executed("t1", "BUY", "AAPL", Decimal(5), Decimal(1000))
    rendered = render(alert, "mainchat", clock.now)
    assert rendered.embed is None
    assert "**Trade executed: BUY AAPL**" in rendered.text


def test_render_truncates_long_title_and_description(clock: FixedClock) -> None:
    alert = templates.system_ready()
    alert.title = "x" * 500
    alert.body = "y" * 5000
    rendered = render(alert, "discord", clock.now)
    assert rendered.embed is not None
    assert len(rendered.embed["title"]) == 256
    assert len(rendered.embed["description"]) == 4096


def test_veto_deadline_is_late_bound_at_render_time() -> None:
    created = datetime(2026, 10, 12, 9, 0, tzinfo=PT)
    alert = templates.veto_window_open("P-2026-10-12-01", 4)
    alert.occurred_at = created
    rendered_now = render(alert, "mainchat", created)
    later = created + timedelta(hours=1)
    rendered_later = render(alert, "mainchat", later)
    assert (created + timedelta(hours=4)).isoformat() in rendered_now.text
    assert (later + timedelta(hours=4)).isoformat() in rendered_later.text
    assert rendered_now.text != rendered_later.text
