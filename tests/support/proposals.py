"""Test helpers for proposals: minimal, self-consistent ``ProposalDoc`` factories."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from stockmanager.domain.docs import (
    CapsSnapshot,
    DeliveryReceipt,
    ProposalDelivery,
    ProposalDoc,
    ProposalLeg,
    ProposalSize,
    TransitionRecord,
)
from stockmanager.domain.proposal_state import GENESIS_HASH, ProposalState, chain_hash
from stockmanager.domain.timeutil import add_elapsed

PT = ZoneInfo("America/Los_Angeles")


def make_caps() -> CapsSnapshot:
    return CapsSnapshot(max_position_pct=Decimal(30), new_position_max_pct=10, proposal_sector_max_pct=80, max_trades_per_cycle=3)


def make_leg(leg: str = "buy1", pid: str = "P-2026-10-12-01") -> ProposalLeg:
    return ProposalLeg(
        leg=leg,
        trade_id=f"{pid}-{leg}",
        pair=None,
        action="BUY",
        ticker="AAPL",
        size=ProposalSize(kind="target_value", target_value=Decimal(1000)),
        est_shares=Decimal(5),
        est_price=Decimal(200),
        est_value=Decimal(1000),
        thesis="t",
    )


def genesis(at: datetime, actor: str = "system:evaluation") -> TransitionRecord:
    rec = TransitionRecord(seq=1, from_state=None, to="PROPOSED", at=at, actor=actor, cause="created", prev_hash=GENESIS_HASH, hash="")
    return rec.model_copy(update={"hash": chain_hash(GENESIS_HASH, rec.model_dump(mode="json", by_alias=True))})


def make_proposal(
    proposal_id: str = "P-2026-10-12-01",
    state: ProposalState = "VETO_WINDOW",
    created_at: datetime = datetime(2026, 10, 12, 7, 0, tzinfo=PT),
    execute_at: datetime | None = None,
    veto_window_hours: int = 4,
    approval_mode: str = "auto",
    transitions: list[TransitionRecord] | None = None,
) -> ProposalDoc:
    execute_at = execute_at or created_at + timedelta(hours=veto_window_hours)
    return ProposalDoc(
        proposal_id=proposal_id,
        created_at=created_at,
        state=state,
        approval_mode=approval_mode,  # type: ignore[arg-type]
        veto_window_hours=veto_window_hours,
        fill_session_date=execute_at.date(),
        execute_at=execute_at,
        expires_at=execute_at + timedelta(minutes=60),
        caps=make_caps(),
        legs=[make_leg(pid=proposal_id)],
        rationale="because",
        hold_alternative="hold instead",
        transitions=transitions or [genesis(created_at)],
    )


def advance(doc: ProposalDoc, to_state: ProposalState, at: datetime, actor: str = "system:sweeper", cause: str = "test") -> ProposalDoc:
    """Append one hash-chained transition to ``doc`` (callers pick a legal ``to_state``)."""
    prev = doc.transitions[-1]
    rec = TransitionRecord(
        seq=prev.seq + 1, from_state=doc.state, to=to_state, at=at, actor=actor, cause=cause, prev_hash=prev.hash, hash=""
    )
    rec = rec.model_copy(update={"hash": chain_hash(prev.hash, rec.model_dump(mode="json", by_alias=True))})
    return doc.model_copy(update={"state": to_state, "transitions": [*doc.transitions, rec]})


def with_receipt(
    doc: ProposalDoc, delivered_at: datetime, *, channel: str = "discord", window_hours: int | None = None
) -> ProposalDoc:
    """``doc`` with one more delivery receipt; the deadline is ``delivered_at`` + the window (AS §8.3)."""
    hours = window_hours if window_hours is not None else doc.veto_window_hours
    receipt = DeliveryReceipt(channel=channel, delivered_at=delivered_at, veto_deadline=add_elapsed(delivered_at, timedelta(hours=hours)))
    return doc.model_copy(update={"delivery": ProposalDelivery(receipts=[*doc.delivery.receipts, receipt])})
