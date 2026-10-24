"""R9 wiring: the ``mainchat`` sender is a live adapter behind the single outbox drain (fail closed when unwired)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from stockmanager.adapters.discord_rest import DiscordRESTChannel
from stockmanager.adapters.hermes_mainchat import HermesMainchatChannel
from stockmanager.app import append_mainchat_channel
from stockmanager.domain.docs import PortfolioDoc
from stockmanager.notify.deliver import ALERT_TTL_HOURS, Channel, Permanent, Rendered, Transient, deliver_pending
from stockmanager.notify.outbox import DEFAULT_CHANNELS, Alert, ChannelState, enqueue
from stockmanager.store import FileStore
from tests.support.books import FakePidChecker, FixedClock
from tests.support.proposals import PT

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=PT)
TOKEN = "mainchat-secret-token"
CHANNEL_ID = 777


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def store(tmp_path: Path, clock: FixedClock) -> FileStore:
    s = FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)
    s.save_portfolio(PortfolioDoc(start_cash=Decimal(10000), cash=Decimal(10000), status="LIVE"))
    return s


def ids(store: FileStore, box: str) -> list[str]:
    return sorted(p.stem for p in (store.root / "alerts" / box).glob("*.json"))


def mainchat_alert(key: str = "m1") -> Alert:
    return Alert("summary", "info", "hello", "body", {}, [], False, key, {}, channels={"mainchat": ChannelState()})


def test_every_default_channel_has_a_live_drain(monkeypatch: pytest.MonkeyPatch, clock: FixedClock) -> None:
    monkeypatch.setenv("MAINCHAT_BOT_TOKEN", TOKEN)
    channels: list[Channel] = [DiscordRESTChannel("discord-token", 555, clock=clock)]
    append_mainchat_channel(channels, CHANNEL_ID, clock=clock)
    names = [c.name for c in channels]
    assert "mainchat" in names and "discord" in names
    assert set(DEFAULT_CHANNELS) <= set(names)
    assert TOKEN not in repr(channels[-1])


@pytest.mark.parametrize(
    ("token", "channel_id"), [(None, None), (TOKEN, None), (None, CHANNEL_ID)], ids=["neither", "no-channel", "no-token"]
)
def test_mainchat_not_wired_without_token_and_channel(
    monkeypatch: pytest.MonkeyPatch, token: str | None, channel_id: int | None
) -> None:
    monkeypatch.delenv("MAINCHAT_BOT_TOKEN", raising=False)
    if token:
        monkeypatch.setenv("MAINCHAT_BOT_TOKEN", token)
    channels: list[Channel] = []
    append_mainchat_channel(channels, channel_id)
    assert channels == []


def test_pending_mainchat_alert_drains_on_first_run_after_wiring(store: FileStore, clock: FixedClock) -> None:
    alert = mainchat_alert()
    enqueue(alert, store)
    assert ids(store, "pending") == [alert.alert_id]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "9001"})

    chan = HermesMainchatChannel(TOKEN, CHANNEL_ID, clock=clock, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert deliver_pending(store, [chan], clock) == [alert.alert_id]
    assert ids(store, "pending") == [] and ids(store, "sent") == [alert.alert_id]
    assert len(seen) == 1 and seen[0].url.path == f"/api/v10/channels/{CHANNEL_ID}/messages"
    assert seen[0].headers["authorization"] == f"Bot {TOKEN}"
    sent = Alert.from_json((store.root / "alerts" / "sent" / f"{alert.alert_id}.json").read_text())
    state = sent.channels["mainchat"]
    assert state.state == "sent" and state.receipt == "9001"
    receipt = chan.send(Rendered("hi"), nonce="n")
    assert receipt.channel == "mainchat" and receipt.thread_id is None
    assert TOKEN not in (store.root / "alerts" / "sent" / f"{alert.alert_id}.json").read_text()


def test_unwired_mainchat_stays_pending_inside_ttl(store: FileStore, clock: FixedClock) -> None:
    alert = mainchat_alert("m2")
    enqueue(alert, store)
    discord = DiscordRESTChannel("t", 1, clock=clock, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    clock.advance(hours=ALERT_TTL_HOURS - 1)
    assert deliver_pending(store, [discord], clock) == []
    assert ids(store, "pending") == [alert.alert_id]
    assert ids(store, "dead") == []


@pytest.mark.parametrize(
    ("response", "exc", "retry_after"),
    [
        (httpx.Response(429, json={"retry_after": 2.5}), Transient, 2.5),
        (httpx.Response(503), Transient, None),
        (httpx.Response(401, text="no"), Permanent, None),
        (httpx.Response(404, text="no"), Permanent, None),
    ],
)
def test_error_semantics_match_discord_with_mainchat_prefix(
    response: httpx.Response, exc: type[Exception], retry_after: float | None, clock: FixedClock
) -> None:
    chan = HermesMainchatChannel(TOKEN, CHANNEL_ID, clock=clock, client=httpx.Client(transport=httpx.MockTransport(lambda r: response)))
    with pytest.raises(exc) as info:
        chan.send(Rendered("hi"), nonce="n")
    assert str(info.value).startswith("mainchat:") and TOKEN not in str(info.value)
    assert getattr(info.value, "retry_after", None) == retry_after
