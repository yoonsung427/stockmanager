"""Receipt-driven veto window and alert backfill (AS §8.3, §8.4, §9.2)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest

from stockmanager.domain.docs import PortfolioDoc, ProposalDoc
from stockmanager.notify.deliver import Permanent, Receipt, Rendered, Transient, deliver_pending
from stockmanager.notify.outbox import Alert, ChannelState, enqueue
from stockmanager.notify.templates import veto_window_open
from stockmanager.scheduler.sweeper import sweep
from stockmanager.store import FileStore
from tests.support.books import FakePidChecker, FixedClock
from tests.support.proposals import PT, make_proposal, with_receipt

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=PT)
PID = "P-2026-10-12-01"


class Chan:
    """A channel that succeeds unless ``error`` is set; remembers what it was asked to send."""

    def __init__(self, name: Literal["discord", "mainchat"], clock: FixedClock) -> None:
        self.name = name
        self.error: Exception | None = None
        self.texts: list[str] = []
        self._clock = clock

    def send(self, rendered: Rendered, *, nonce: str) -> Receipt:
        if self.error is not None:
            raise self.error
        self.texts.append(json.dumps(rendered.embed) if rendered.embed else rendered.text)
        return Receipt(self.name, nonce, self._clock())


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def store(tmp_path: Path, clock: FixedClock) -> FileStore:
    s = FileStore(tmp_path / "ws", clock=clock, pid_checker=FakePidChecker(), sleep=lambda _s: None)
    s.save_portfolio(PortfolioDoc(start_cash=Decimal(10000), cash=Decimal(10000), status="LIVE"))
    return s


def proposed(store: FileStore, **kw: object) -> ProposalDoc:
    base: dict[str, object] = {
        "state": "PROPOSED",
        "created_at": NOW,
        "veto_window_hours": 4,
        "execute_at": NOW + timedelta(hours=24),
        "proposal_id": PID,
    }
    doc = make_proposal(**{**base, **kw})  # type: ignore[arg-type]
    store.save_proposal(doc.proposal_id, doc)
    return doc


def load(store: FileStore, pid: str = PID) -> ProposalDoc:
    return ProposalDoc.model_validate_json(store.proposal_path(pid).read_text(encoding="utf-8"))


def queue_alert(store: FileStore, pid: str = PID, hours: int = 4, channels: tuple[str, ...] = ("discord", "mainchat")) -> Alert:
    alert = veto_window_open(pid, hours)
    alert.channels = {c: ChannelState() for c in channels}
    assert enqueue(alert, store)
    return alert


def alert_keys(store: FileStore, box: str) -> list[str]:
    return [
        json.loads(p.read_text())["dedupe_key"] for p in sorted((store.root / "alerts" / box).glob("*.json"))
    ] if (store.root / "alerts" / box).exists() else []


# -- sweeper: receipt-driven transition ---------------------------------------------------------------------------


def test_proposed_stays_proposed_without_a_receipt(store: FileStore) -> None:
    proposed(store)
    assert sweep(NOW, store, store.clock) == []
    doc = load(store)
    assert doc.state == "PROPOSED" and doc.veto_deadline is None


def test_receipt_opens_window_with_the_receipts_deadline(store: FileStore) -> None:
    delivered = NOW - timedelta(minutes=30)
    receipted = with_receipt(proposed(store), delivered)
    store.save_proposal(PID, receipted)

    sweep(NOW, store, store.clock)

    doc = load(store)
    assert doc.state == "VETO_WINDOW"
    assert doc.veto_deadline == delivered + timedelta(hours=4) != NOW + timedelta(hours=4)
    assert doc.transitions[-1].actor == "system:sweeper" and doc.transitions[-1].cause == "delivered"
    assert doc.delivery.receipts == receipted.delivery.receipts


def test_receipt_from_a_non_visible_channel_does_not_count(store: FileStore) -> None:
    store.save_proposal(PID, with_receipt(proposed(store), NOW, channel="mainchat"))
    sweep(NOW, store, store.clock)
    assert load(store).state == "PROPOSED"
    sweep(NOW, store, store.clock, visible_channels=["discord", "mainchat"])
    assert load(store).state == "VETO_WINDOW"


def test_first_visible_receipt_determines_the_deadline(store: FileStore) -> None:
    doc = with_receipt(proposed(store), NOW - timedelta(hours=1), channel="mainchat")  # not visible: skipped
    doc = with_receipt(doc, NOW - timedelta(minutes=20), channel="discord")
    doc = with_receipt(doc, NOW - timedelta(minutes=5), channel="other")
    store.save_proposal(PID, doc)
    sweep(NOW, store, store.clock, visible_channels=["discord", "other"])
    assert load(store).veto_deadline == NOW - timedelta(minutes=20) + timedelta(hours=4)


def test_sweep_is_idempotent_with_receipts(store: FileStore) -> None:
    store.save_proposal(PID, with_receipt(proposed(store), NOW))
    sweep(NOW, store, store.clock)
    first = store.proposal_path(PID).read_text()
    assert sweep(NOW, store, store.clock) == []
    assert store.proposal_path(PID).read_text() == first


# -- D10 guard ------------------------------------------------------------------------------------------------------


def test_auto_mode_late_delivery_expires_with_an_alert(store: FileStore) -> None:
    execute_at = NOW + timedelta(hours=2)  # the 4 h window would close after the fill
    store.save_proposal(PID, with_receipt(proposed(store, execute_at=execute_at, approval_mode="auto"), NOW))
    sweep(NOW, store, store.clock)
    doc = load(store)
    assert (doc.state, doc.transitions[-1].cause) == ("EXPIRED", "late_delivery")
    assert alert_keys(store, "pending") == [f"expired:{PID}"]  # the re-confirm notice (AS §8.3 item 5)


def test_auto_mode_deadline_exactly_at_execute_at_is_not_late(store: FileStore) -> None:
    store.save_proposal(PID, with_receipt(proposed(store, execute_at=NOW + timedelta(hours=4)), NOW))
    sweep(NOW, store, store.clock)
    assert load(store).state == "VETO_WINDOW"


def test_explicit_mode_expires_only_when_delivered_at_or_after_execute_at(store: FileStore) -> None:
    execute_at = NOW + timedelta(hours=2)
    early = proposed(store, execute_at=execute_at, approval_mode="explicit")
    store.save_proposal(PID, with_receipt(early, NOW))  # deadline after execute_at, but delivered before: fine
    sweep(NOW, store, store.clock)
    assert load(store).state == "VETO_WINDOW"

    other = make_proposal(
        proposal_id="P-2026-10-12-02", state="PROPOSED", created_at=NOW, approval_mode="explicit", execute_at=execute_at
    )
    store.save_proposal(other.proposal_id, with_receipt(other, execute_at))
    sweep(execute_at, store, store.clock)
    doc = load(store, other.proposal_id)
    assert (doc.state, doc.transitions[-1].cause) == ("EXPIRED", "late_delivery")


# -- never delivered ------------------------------------------------------------------------------------------------


def test_never_delivered_stays_proposed_until_expires_at(store: FileStore) -> None:
    doc = proposed(store)
    sweep(doc.expires_at, store, store.clock)
    assert load(store).state == "PROPOSED"
    sweep(doc.expires_at + timedelta(minutes=1), store, store.clock)
    final = load(store)
    assert (final.state, final.transitions[-1].cause) == ("EXPIRED", "past expires_at")
    assert [t.to for t in final.transitions] == ["PROPOSED", "EXPIRED"]


# -- alert backfill -------------------------------------------------------------------------------------------------


def test_missing_veto_alert_is_backfilled_once(store: FileStore) -> None:
    proposed(store)
    assert sweep(NOW, store, store.clock) == []  # no transition, but the alert is written
    assert alert_keys(store, "pending") == [f"veto_window_open:{PID}"]
    sweep(NOW, store, store.clock)
    sweep(NOW, store, store.clock)
    assert alert_keys(store, "pending") == [f"veto_window_open:{PID}"]


@pytest.mark.parametrize("box", ["pending", "sent", "dead"])
def test_existing_alert_in_any_box_suppresses_backfill(store: FileStore, box: str) -> None:
    proposed(store)
    queue_alert(store)
    if box != "pending":
        src = next((store.root / "alerts" / "pending").glob("*.json"))
        (store.root / "alerts" / box).mkdir(parents=True, exist_ok=True)
        src.rename(store.root / "alerts" / box / src.name)
    sweep(NOW, store, store.clock)
    assert alert_keys(store, "pending") == ([f"veto_window_open:{PID}"] if box == "pending" else [])


def test_no_backfill_once_the_window_is_open_or_for_terminal_proposals(store: FileStore) -> None:
    store.save_proposal(PID, with_receipt(proposed(store), NOW))
    done = make_proposal(proposal_id="P-2026-10-12-02", state="VETOED", created_at=NOW)
    store.save_proposal(done.proposal_id, done)
    sweep(NOW, store, store.clock)
    assert alert_keys(store, "pending") == []


def test_no_backfill_for_pending_reconfirm_replacement(store: FileStore) -> None:
    doc = proposed(store).model_copy(update={"reconfirm_check": "pending"})
    store.save_proposal(PID, doc)
    sweep(NOW, store, store.clock)
    assert alert_keys(store, "pending") == []


# -- backward compatibility -----------------------------------------------------------------------------------------


def test_proposal_file_without_delivery_field_is_treated_as_no_receipts(store: FileStore) -> None:
    data = json.loads(proposed(store).to_canonical_json())
    del data["delivery"]
    store.proposal_path(PID).write_text(json.dumps(data), encoding="utf-8")
    assert load(store).delivery.receipts == []
    sweep(NOW, store, store.clock)
    assert load(store).state == "PROPOSED"
    assert alert_keys(store, "pending") == [f"veto_window_open:{PID}"]


def test_delivery_field_round_trips_through_canonical_json(store: FileStore) -> None:
    doc = with_receipt(proposed(store), NOW)
    assert ProposalDoc.model_validate_json(doc.to_canonical_json()) == doc
    store.save_proposal(PID, doc)
    assert json.loads(store.proposal_path(PID).read_text())["delivery"]["receipts"][0]["channel"] == "discord"


# -- deliverer: receipts --------------------------------------------------------------------------------------------


def test_successful_visible_send_records_a_receipt(store: FileStore, clock: FixedClock) -> None:
    proposed(store)
    queue_alert(store)
    discord, chat = Chan("discord", clock), Chan("mainchat", clock)

    deliver_pending(store, [discord, chat], clock)

    receipts = load(store).delivery.receipts
    assert [(r.channel, r.delivered_at, r.veto_deadline) for r in receipts] == [("discord", NOW, NOW + timedelta(hours=4))]
    assert (NOW + timedelta(hours=4)).isoformat() in discord.texts[0]


def test_receipt_then_sweep_opens_the_window_end_to_end(store: FileStore, clock: FixedClock) -> None:
    proposed(store)
    queue_alert(store)
    deliver_pending(store, [Chan("discord", clock)], clock)
    sweep(NOW, store, store.clock)
    doc = load(store)
    assert doc.state == "VETO_WINDOW" and doc.veto_deadline == NOW + timedelta(hours=4)


def test_non_visible_channel_records_no_receipt(store: FileStore, clock: FixedClock) -> None:
    proposed(store)
    queue_alert(store)
    deliver_pending(store, [Chan("mainchat", clock)], clock)
    assert load(store).delivery.receipts == []


def test_visible_channels_come_from_the_caller(store: FileStore, clock: FixedClock) -> None:
    proposed(store)
    queue_alert(store)
    deliver_pending(store, [Chan("mainchat", clock)], clock, visible_channels=["mainchat"])
    assert [r.channel for r in load(store).delivery.receipts] == ["mainchat"]


@pytest.mark.parametrize("error", [Transient("down"), Permanent("401")])
def test_failed_send_records_no_receipt(store: FileStore, clock: FixedClock, error: Exception) -> None:
    proposed(store)
    queue_alert(store)
    discord = Chan("discord", clock)
    discord.error = error
    deliver_pending(store, [discord], clock)
    assert load(store).delivery.receipts == []


def test_replay_after_an_outage_gets_a_deadline_relative_to_delivery(store: FileStore, clock: FixedClock) -> None:
    proposed(store)
    queue_alert(store)
    discord = Chan("discord", clock)
    discord.error = Transient("down")
    deliver_pending(store, [discord], clock)
    discord.error = None
    clock.advance(hours=3)  # past the back-off
    deliver_pending(store, [discord], clock)
    (receipt,) = load(store).delivery.receipts
    assert receipt.delivered_at == NOW + timedelta(hours=3)
    assert receipt.veto_deadline == NOW + timedelta(hours=7)


def test_second_visible_channel_reuses_the_first_deadline(store: FileStore, clock: FixedClock) -> None:
    proposed(store)
    queue_alert(store)
    discord, chat = Chan("discord", clock), Chan("mainchat", clock)
    chat.error = Transient("down")
    visible = ["discord", "mainchat"]
    deliver_pending(store, [discord, chat], clock, visible_channels=visible)
    chat.error = None
    clock.advance(minutes=30)
    deliver_pending(store, [discord, chat], clock, visible_channels=visible)

    first, second = load(store).delivery.receipts
    assert (first.channel, second.channel) == ("discord", "mainchat")
    assert second.delivered_at == NOW + timedelta(minutes=30)
    assert second.veto_deadline == first.veto_deadline == NOW + timedelta(hours=4)
    assert first.veto_deadline.isoformat() in chat.texts[0]  # the later channel is shown the fixed deadline too

    sweep(clock(), store, store.clock, visible)
    assert load(store).veto_deadline == NOW + timedelta(hours=4)


def test_a_resend_does_not_duplicate_a_receipt(store: FileStore, clock: FixedClock) -> None:
    proposed(store)
    alert = queue_alert(store, channels=("discord",))
    discord = Chan("discord", clock)
    deliver_pending(store, [discord], clock)
    # simulate a crash that lost the 'sent' bookkeeping: the alert is back in pending and is re-sent
    sent = store.root / "alerts" / "sent" / f"{alert.alert_id}.json"
    (store.root / "alerts" / "pending").joinpath(sent.name).write_text(
        sent.read_text().replace('"state": "sent"', '"state": "pending"')
    )
    clock.advance(minutes=1)
    deliver_pending(store, [discord], clock)
    assert len(load(store).delivery.receipts) == 1


def test_receipt_is_not_recorded_on_a_terminal_proposal(store: FileStore, clock: FixedClock) -> None:
    store.save_proposal(PID, make_proposal(proposal_id=PID, state="VETOED", created_at=NOW))
    queue_alert(store)
    deliver_pending(store, [Chan("discord", clock)], clock)
    assert load(store).delivery.receipts == []


def test_missing_proposal_does_not_break_delivery(store: FileStore, clock: FixedClock) -> None:
    queue_alert(store, pid="P-GONE")
    discord = Chan("discord", clock)
    assert len(deliver_pending(store, [discord], clock)) == 1


def test_busy_lock_defers_the_send_instead_of_losing_the_receipt(store: FileStore, clock: FixedClock) -> None:
    proposed(store)
    queue_alert(store, channels=("discord",))
    discord = Chan("discord", clock)
    with store.transaction("other"):  # holds portfolio.lock
        assert deliver_pending(store, [discord], clock) == []
    assert load(store).delivery.receipts == []
    assert len(list((store.root / "alerts" / "pending").glob("*.json"))) == 1  # still queued, retried later

    clock.advance(minutes=1)
    assert len(deliver_pending(store, [discord], clock)) == 1
    assert [r.channel for r in load(store).delivery.receipts] == ["discord"]


def test_record_receipt_does_not_clobber_a_concurrent_sweeper_transition(store: FileStore, clock: FixedClock) -> None:
    """The receipt is written under portfolio.lock, so it reads the sweeper's latest file, not a stale copy."""
    store.save_proposal(PID, with_receipt(proposed(store), NOW, channel="discord"))
    sweep(NOW, store, store.clock)  # -> VETO_WINDOW
    queue_alert(store)
    deliver_pending(store, [Chan("mainchat", clock)], clock, visible_channels=["discord", "mainchat"])
    doc = load(store)
    assert doc.state == "VETO_WINDOW" and [t.to for t in doc.transitions] == ["PROPOSED", "VETO_WINDOW"]
    assert [r.channel for r in doc.delivery.receipts] == ["discord", "mainchat"]
