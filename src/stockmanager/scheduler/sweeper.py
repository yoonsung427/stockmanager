"""Proposal sweeper (AS §1.5): idempotent convergence of every proposal, one transaction per tick.

The veto window is receipt-driven (AS §8.3): a ``PROPOSED`` proposal moves to ``VETO_WINDOW`` only once the deliverer
has appended a receipt from a visible channel to ``delivery.receipts``, and ``veto_deadline`` is that receipt's.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime

from stockmanager.domain.docs import DeliveryReceipt, ProposalDoc, TransitionRecord
from stockmanager.domain.proposal_state import ProposalState, can_transition, chain_hash, is_terminal
from stockmanager.notify.outbox import prepare, raised_dedupe_keys
from stockmanager.notify.templates import proposal_expired, veto_window_open
from stockmanager.store import FileStore
from stockmanager.store.lock import Clock, LockBusy

DEFAULT_VISIBLE_CHANNELS: tuple[str, ...] = ("discord",)


def _transition(doc: ProposalDoc, to_state: ProposalState, actor: str, cause: str, at: datetime) -> ProposalDoc:
    prev = doc.transitions[-1]
    rec = TransitionRecord(
        seq=prev.seq + 1, from_state=doc.state, to=to_state, at=at, actor=actor, cause=cause, prev_hash=prev.hash, hash=""
    )
    rec = rec.model_copy(update={"hash": chain_hash(prev.hash, rec.model_dump(mode="json", by_alias=True))})
    return doc.model_copy(update={"state": to_state, "transitions": [*doc.transitions, rec]})


def _load_all_proposals(store: FileStore) -> list[ProposalDoc]:
    directory = store.root / "proposals"
    if not directory.exists():
        return []
    out: list[ProposalDoc] = []
    for path in sorted(directory.glob("*.json")):
        try:
            out.append(ProposalDoc.model_validate_json(path.read_text(encoding="utf-8")))
        except ValueError:
            continue  # quarantined: unparsable files are excluded from every job (AS §10.1)
    return out


def _first_visible_receipt(doc: ProposalDoc, visible_channels: Sequence[str]) -> DeliveryReceipt | None:
    """The first receipt in ``doc`` from a channel in ``visible_channels``, or ``None`` (AS §8.3 item 3)."""
    return next((r for r in doc.delivery.receipts if r.channel in visible_channels), None)


def _plan(
    now: datetime,
    status: str,
    proposals: list[ProposalDoc],
    visible_channels: Sequence[str] = DEFAULT_VISIBLE_CHANNELS,
    announced: Collection[str] = frozenset(),
) -> tuple[dict[str, ProposalDoc], dict[str, str], list[TransitionRecord]]:
    """Pure sweeper rules over ``proposals``: the staged proposals, staged alert files and transitions to write.

    ``announced`` is every ``dedupe_key`` ever queued (pending, sent or dead); a ``PROPOSED`` proposal whose
    ``veto_window_open:<id>`` is not in it gets that alert re-derived (AS §8.3 item 7).
    """
    staged: dict[str, ProposalDoc] = {}
    alerts: dict[str, str] = {}
    written: list[TransitionRecord] = []

    def apply(doc: ProposalDoc, to_state: ProposalState, actor: str, cause: str) -> ProposalDoc:
        new_doc = _transition(doc, to_state, actor, cause, now)
        staged[new_doc.proposal_id] = new_doc
        written.append(new_doc.transitions[-1])
        return new_doc

    for original in proposals:
        cur = original
        if status == "PAUSED" and can_transition(cur.state, "CANCELLED_PAUSED"):
            cur = apply(cur, "CANCELLED_PAUSED", "system:pause", "system paused (sweeper convergence)")
        elif status == "CLOSED" and can_transition(cur.state, "CANCELLED_CLOSED"):
            cur = apply(cur, "CANCELLED_CLOSED", "system:close", "system closed (sweeper convergence)")

        if cur.reconfirm_check == "pending" and not is_terminal(cur.state):
            continue  # a RECONFIRM replacement is not delivered and runs no clock until its job validates it (AS §8.7)

        if cur.state == "PROPOSED" and can_transition(cur.state, "VETO_WINDOW"):
            receipt = _first_visible_receipt(cur, visible_channels)
            if receipt is None:
                key = f"veto_window_open:{cur.proposal_id}"
                if key not in announced:  # a crash between the commit and the alert enqueue must not strand it
                    alert = prepare(veto_window_open(cur.proposal_id, cur.veto_window_hours), now)
                    alerts[f"{alert.alert_id}.json"] = alert.to_json()
            else:
                opened = cur.model_copy(update={"veto_deadline": receipt.veto_deadline})
                late = (
                    receipt.veto_deadline > cur.execute_at
                    if cur.approval_mode == "auto"
                    else receipt.delivered_at >= cur.execute_at
                )
                if late:
                    cur = apply(opened, "EXPIRED", "system:sweeper", "late_delivery")
                    alert = prepare(proposal_expired(cur.proposal_id, "delivered too late to veto before execution"), now)
                    alerts[f"{alert.alert_id}.json"] = alert.to_json()
                else:
                    cur = apply(opened, "VETO_WINDOW", "system:sweeper", "delivered")

        if (
            cur.state == "VETO_WINDOW"
            and cur.approval_mode == "auto"
            and cur.veto_deadline is not None
            and cur.veto_deadline <= now
            and can_transition(cur.state, "APPROVED")
        ):
            cur = apply(cur, "APPROVED", "system:sweeper", "auto-approved at deadline")

        if not is_terminal(cur.state) and now > cur.expires_at and can_transition(cur.state, "EXPIRED"):
            cur = apply(cur, "EXPIRED", "system:sweeper", "past expires_at")
            alert = prepare(proposal_expired(cur.proposal_id, "missed execution window"), now)
            alerts[f"{alert.alert_id}.json"] = alert.to_json()
    return staged, alerts, written


def sweep(
    now: datetime, store: FileStore, clock: Clock, visible_channels: Sequence[str] = DEFAULT_VISIBLE_CHANNELS
) -> list[TransitionRecord]:
    """Run every sweeper rule once (AS §1.5 steps 1-5), under one transaction only if anything changes. Idempotent.

    1. ``PAUSED``/``CLOSED`` -> every non-terminal proposal -> ``CANCELLED_PAUSED``/``CANCELLED_CLOSED`` (TS D19).
    2. ``PROPOSED`` with a receipt from one of ``visible_channels`` -> ``VETO_WINDOW``, deadline = the first such
       receipt's. Without one it stays ``PROPOSED`` and a missing ``veto_window_open`` alert is re-enqueued.
    3. ``VETO_WINDOW``, ``auto`` mode, deadline passed -> ``APPROVED``.
    4. Non-terminal, past ``expires_at`` -> ``EXPIRED`` (alert).
    5. A receipt that comes too late (``auto``: deadline after ``execute_at``; ``explicit``: delivered at or after
       ``execute_at``) -> ``EXPIRED``, cause ``late_delivery`` (TS D10, AS §8.3 item 5), instead of step 2.

    The rules are first evaluated lock-free (every file is written atomically, so a read is never torn); the
    common "nothing to do" tick returns without touching ``portfolio.lock``. If there is work, it is evaluated
    again from fresh state under the lock, which is the only evaluation whose result is written (AS §1.5).

    Returns the transition records written this call.
    """
    if not store.portfolio_exists():
        return []
    staged, alerts, _ = _plan(
        now, store.load_portfolio().status, _load_all_proposals(store), visible_channels, raised_dedupe_keys(store.root)
    )
    if not staged and not alerts:
        return []
    try:
        with store.transaction("sweep") as txn:
            staged, alerts, written = _plan(
                now, txn.state.status, _load_all_proposals(store), visible_channels, raised_dedupe_keys(store.root)
            )
            if staged:
                txn.stage_proposals(dict(staged))
            if alerts:
                txn.stage_alerts(alerts)
    except LockBusy:
        return []
    return written
