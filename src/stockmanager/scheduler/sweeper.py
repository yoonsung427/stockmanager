"""Proposal sweeper (AS §1.5): idempotent convergence of every proposal, one transaction per tick.

Deviation from the architecture spec: this build's ``ProposalDoc`` carries no ``delivery``/receipt field (AS §8.3),
so the veto window is treated as opening on the first sweep after creation (an approximation of "delivered
immediately") rather than being gated on a channel receipt.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from stockmanager.domain.docs import ProposalDoc, TransitionRecord
from stockmanager.domain.proposal_state import ProposalState, can_transition, chain_hash, is_terminal
from stockmanager.notify.outbox import prepare
from stockmanager.notify.templates import proposal_expired
from stockmanager.store import FileStore
from stockmanager.store.lock import Clock, LockBusy


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


def sweep(now: datetime, store: FileStore, clock: Clock) -> list[TransitionRecord]:
    """Run every sweeper rule once (AS §1.5 steps 1-5), under one transaction if anything changes. Idempotent.

    1. ``PAUSED``/``CLOSED`` -> every non-terminal proposal -> ``CANCELLED_PAUSED``/``CANCELLED_CLOSED`` (TS D19).
    2. ``PROPOSED`` -> ``VETO_WINDOW`` (deadline = now + veto_window_hours; see module docstring).
    3. ``VETO_WINDOW``, ``auto`` mode, deadline passed -> ``APPROVED``.
    4. Non-terminal, past ``expires_at`` -> ``EXPIRED`` (alert).
    5. ``auto`` proposals whose window would close after ``execute_at`` -> ``EXPIRED`` immediately (TS D10).

    Returns the transition records written this call.
    """
    if not store.portfolio_exists():
        return []
    staged: dict[str, ProposalDoc] = {}
    alerts: dict[str, str] = {}
    written: list[TransitionRecord] = []

    def apply(doc: ProposalDoc, to_state: ProposalState, actor: str, cause: str) -> ProposalDoc:
        new_doc = _transition(doc, to_state, actor, cause, now)
        staged[new_doc.proposal_id] = new_doc
        written.append(new_doc.transitions[-1])
        return new_doc

    try:
        with store.transaction("sweep") as txn:
            status = txn.state.status
            for original in _load_all_proposals(store):
                cur = original
                if status == "PAUSED" and can_transition(cur.state, "CANCELLED_PAUSED"):
                    cur = apply(cur, "CANCELLED_PAUSED", "system:pause", "system paused (sweeper convergence)")
                elif status == "CLOSED" and can_transition(cur.state, "CANCELLED_CLOSED"):
                    cur = apply(cur, "CANCELLED_CLOSED", "system:close", "system closed (sweeper convergence)")

                if cur.state == "PROPOSED" and can_transition(cur.state, "VETO_WINDOW"):
                    deadline = now + timedelta(hours=cur.veto_window_hours)
                    opened = cur.model_copy(update={"veto_deadline": deadline})
                    if cur.approval_mode == "auto" and deadline > cur.execute_at:
                        cur = apply(opened, "EXPIRED", "system:sweeper", "late_delivery")
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
            if staged:
                txn.stage_proposals(dict(staged))
            if alerts:
                txn.stage_alerts(alerts)
    except LockBusy:
        return []
    return written
