"""Command handlers (AS §1.7): parse, authorize and execute. Every path returns a reply string (TS CMD-X01).

Each handler is one ``Transaction`` where it mutates the books or proposals (AS §8.5); nothing here opens a file
directly except the small non-books state file ``state/pending_close.json`` that AS
§4.2's proposed amendment excludes from "the books" (⚑AD9).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from stockmanager.commands.auth import Sender, authorize
from stockmanager.commands.parser import PROPOSAL_ID_RE, parse_command
from stockmanager.config import BLOCKED_SET_PREFIXES, SETTABLE_KEYS, Config
from stockmanager.domain import ids as domain_ids
from stockmanager.domain.docs import ProposalDoc
from stockmanager.domain.proposal_state import GENESIS_HASH, ProposalState, can_transition, chain_hash, is_terminal
from stockmanager.domain.timeutil import add_elapsed
from stockmanager.execution.engine import ExecutionEngine
from stockmanager.notify import prepare as prepare_alert
from stockmanager.notify.templates import system_pause_resume
from stockmanager.store import FileStore, fsutil
from stockmanager.store.lock import PT, LockBusy
from stockmanager.store.reconcile import format_log_line
from stockmanager.store.transaction import GateError, ReconcileHalt

if TYPE_CHECKING:
    from stockmanager.domain.docs import TransitionRecord
    from stockmanager.domain.ports import PriceProvider, SessionCalendar
    from stockmanager.notify.outbox import Alert
    from stockmanager.store.lock import Clock
    from stockmanager.store.transaction import Transaction

MAX_MESSAGE_CHARS = 2000
CLOSE_CONFIRM_WINDOW = timedelta(minutes=10)


#: The reply ``handle`` gives text that is not a command; the Discord bot tries stock queries on exactly this.
UNRECOGNIZED = "❌ Unrecognized command"


@dataclass
class CommandContext:
    """What a handler needs to act: the store, the live config, time, and the price/calendar seams."""

    store: FileStore
    cfg: Config
    clock: Clock
    calendar: SessionCalendar
    price_provider: PriceProvider
    price_fallback: PriceProvider | None = None  # ``None``: the primary is retried in its place


def handle(text: str, sender: Sender, ctx: CommandContext) -> str:
    """Parse, authorize, and execute a command. Always returns a reply string (TS CMD-X01)."""
    normalized = text.strip()
    if len(normalized) > MAX_MESSAGE_CHARS:
        return "❌ Message too long"
    parsed = parse_command(normalized)
    if parsed is None:
        return UNRECOGNIZED
    auth = authorize(sender, parsed, ctx.cfg)
    if not auth.ok:
        return f"❌ {auth.reason}"
    if parsed.verb == "VETO":
        return cmd_veto(ctx, parsed.target, parsed.reason, sender)
    if parsed.verb == "APPROVE":
        if parsed.target == "OPENING":
            return cmd_approve_opening(ctx, sender)
        return cmd_approve(ctx, parsed.target, sender)
    if parsed.verb == "PAUSE":
        return cmd_pause(ctx, sender)
    if parsed.verb == "RESUME":
        return cmd_resume(ctx, sender)
    if parsed.verb == "CLOSE":
        return cmd_close(ctx, sender)
    if parsed.verb == "CLOSE_CONFIRM":
        return cmd_close_confirm(ctx, sender)
    if parsed.verb == "RECONFIRM":
        return cmd_reconfirm(ctx, parsed.target, sender)
    if parsed.verb == "RECONCILE":
        return cmd_reconcile(ctx, parsed.target or "readonly", sender)
    if parsed.verb == "STATUS":
        return cmd_status(ctx, sender)
    if parsed.verb == "SET":
        return cmd_set(ctx, parsed.target, parsed.reason, sender)
    return UNRECOGNIZED  # pragma: no cover - parse_command only yields the verbs handled above


# -- shared helpers ---------------------------------------------------------------------------------------------


def _actor(sender: Sender) -> str:
    return "cli" if sender.kind == "cli" else f"discord:{sender.user_id}"


def _genesis_transition(at: datetime, actor: str, cause: str) -> TransitionRecord:
    from stockmanager.domain.docs import TransitionRecord as _TransitionRecord

    rec = _TransitionRecord(seq=1, from_state=None, to="PROPOSED", at=at, actor=actor, cause=cause, prev_hash=GENESIS_HASH, hash="")
    return rec.model_copy(update={"hash": chain_hash(GENESIS_HASH, rec.model_dump(mode="json", by_alias=True))})


def _transition(doc: ProposalDoc, to_state: ProposalState, actor: str, cause: str, at: datetime) -> ProposalDoc:
    """Append one legal transition (AS §8.4) and return the updated ``ProposalDoc``."""
    from stockmanager.domain.docs import TransitionRecord as _TransitionRecord

    prev = doc.transitions[-1]
    rec = _TransitionRecord(
        seq=prev.seq + 1, from_state=doc.state, to=to_state, at=at, actor=actor, cause=cause, prev_hash=prev.hash, hash=""
    )
    rec = rec.model_copy(update={"hash": chain_hash(prev.hash, rec.model_dump(mode="json", by_alias=True))})
    return doc.model_copy(update={"state": to_state, "transitions": [*doc.transitions, rec]})


def _load_proposal(store: FileStore, proposal_id: str) -> ProposalDoc | None:
    path = store.proposal_path(proposal_id)
    if not path.exists():
        return None
    return ProposalDoc.model_validate_json(path.read_text(encoding="utf-8"))


def _load_all_proposals(store: FileStore) -> list[ProposalDoc]:
    directory = store.root / "proposals"
    if not directory.exists():
        return []
    out: list[ProposalDoc] = []
    for path in sorted(directory.glob("*.json")):
        try:
            out.append(ProposalDoc.model_validate_json(path.read_text(encoding="utf-8")))
        except ValueError:
            continue  # quarantined: unparsable proposal files are excluded from every job (AS §10.1)
    return out


def _stage_alert(txn: Transaction, alert: Alert, now: datetime) -> None:
    prepared = prepare_alert(alert, now)
    txn.stage_alerts({f"{prepared.alert_id}.json": prepared.to_json()})


# -- VETO / APPROVE ----------------------------------------------------------------------------------------------


def cmd_veto(ctx: CommandContext, proposal_id: str | None, reason: str | None, sender: Sender) -> str:
    """Veto a proposal (AS §8.4). Uses ``sender.sent_at``, never "now" (TS D11)."""
    if proposal_id is None or not PROPOSAL_ID_RE.fullmatch(proposal_id):
        return "❌ VETO needs a valid proposal ID, e.g. VETO P-2026-10-24-01"
    try:
        with ctx.store.transaction("cmd:veto") as txn:
            doc = _load_proposal(ctx.store, proposal_id)
            if doc is None:
                return f"❌ No such proposal: {proposal_id}"
            if not can_transition(doc.state, "VETOED"):
                return f"❌ Cannot VETO {proposal_id}: it is {doc.state}"
            new_doc = _transition(doc, "VETOED", _actor(sender), reason or "vetoed", sender.sent_at)
            txn.stage_proposals({proposal_id: new_doc})
    except LockBusy:
        return "❌ System busy"
    return f"✅ Vetoed {proposal_id}."


def cmd_approve(ctx: CommandContext, proposal_id: str | None, sender: Sender) -> str:
    """Approve a proposal (explicit mode only; must be before ``execute_at``)."""
    if proposal_id is None or not PROPOSAL_ID_RE.fullmatch(proposal_id):
        return "❌ APPROVE needs a valid proposal ID, e.g. APPROVE P-2026-10-24-01"
    try:
        with ctx.store.transaction("cmd:approve") as txn:
            doc = _load_proposal(ctx.store, proposal_id)
            if doc is None:
                return f"❌ No such proposal: {proposal_id}"
            if doc.approval_mode != "explicit":
                return f"❌ {proposal_id} is auto-approved; there is nothing to approve"
            if not can_transition(doc.state, "APPROVED"):
                return f"❌ Cannot APPROVE {proposal_id}: it is {doc.state}"
            if sender.sent_at >= doc.execute_at:
                return f"❌ Cannot APPROVE {proposal_id}: its execution time has passed"
            new_doc = _transition(doc, "APPROVED", _actor(sender), "approved", sender.sent_at)
            txn.stage_proposals({proposal_id: new_doc})
    except LockBusy:
        return "❌ System busy"
    return f"✅ Approved {proposal_id}."


def cmd_approve_opening(ctx: CommandContext, sender: Sender) -> str:
    """``APPROVE OPENING``: ``STAGED`` -> ``APPROVED`` (UX §2, TS CMD-A03/A04/A08). Fills nothing.

    The opening job fills the staged picks at the next session open (AS §1.3 ``run_opening``); this only records the
    owner's consent, which is why the reply names the open and not a fill.
    """
    if not ctx.store.portfolio_exists():
        return "❌ Nothing to approve: no portfolio yet"
    try:
        with ctx.store.transaction("cmd:approve_opening") as txn:
            status = txn.state.status
            if status != "STAGED":
                return f"❌ Nothing to approve (status {status})"
            if not txn.state.config.get("staged_orders"):
                return "❌ No staged picks"
            txn.stage_portfolio(txn.state.model_copy(update={"status": "APPROVED"}))
            opens_at = ctx.calendar.next_session_open_at_or_after(sender.sent_at)
    except LockBusy:
        return "❌ System busy"
    return f"✅ Opening approved. The picks fill at the next open, {opens_at:%a %Y-%m-%d %H:%M} PT."


# -- PAUSE / RESUME -----------------------------------------------------------------------------------------------


def cmd_pause(ctx: CommandContext, sender: Sender) -> str:
    """Pause the system: one transaction cancels every non-terminal proposal (TS NN-33)."""
    cancelled: list[str] = []
    try:
        with ctx.store.transaction("cmd:pause") as txn:
            txn.require(status="LIVE")
            now = ctx.clock()
            staged: dict[str, ProposalDoc] = {}
            for doc in _load_all_proposals(ctx.store):
                if is_terminal(doc.state):
                    continue
                staged[doc.proposal_id] = _transition(doc, "CANCELLED_PAUSED", _actor(sender), "system paused", now)
                cancelled.append(doc.proposal_id)
            txn.stage_portfolio(txn.state.model_copy(update={"status": "PAUSED"}))
            txn.stage_proposals(staged)
            alert = system_pause_resume("pause")
            if cancelled:
                alert.body += f" Cancelled proposals: {', '.join(cancelled)}."
            _stage_alert(txn, alert, now)
    except GateError as exc:
        return f"❌ {exc}"
    except LockBusy:
        return "❌ System busy"
    suffix = f" Cancelled: {', '.join(cancelled)}." if cancelled else ""
    return f"✅ System paused.{suffix}"


def cmd_resume(ctx: CommandContext, sender: Sender) -> str:
    """Resume the system: only restores ``LIVE``; cancelled proposals stay cancelled (they are terminal)."""
    try:
        with ctx.store.transaction("cmd:resume") as txn:
            txn.require(status="PAUSED")
            now = ctx.clock()
            txn.stage_portfolio(txn.state.model_copy(update={"status": "LIVE"}))
            _stage_alert(txn, system_pause_resume("resume"), now)
    except GateError as exc:
        return f"❌ {exc}"
    except LockBusy:
        return "❌ System busy"
    return "✅ System resumed."


# -- CLOSE / CLOSE CONFIRM ------------------------------------------------------------------------------------------


def _pending_close_path(store: FileStore) -> Path:
    return store.root / "state" / "pending_close.json"


def cmd_close(ctx: CommandContext, sender: Sender) -> str:
    """Request a close: records ``state/pending_close.json``; ``CLOSE CONFIRM`` executes it (AS §1.7)."""
    if not ctx.store.portfolio_exists():
        return "❌ No portfolio to close"
    portfolio = ctx.store.load_portfolio()
    if portfolio.status not in ("LIVE", "PAUSED"):
        return f"❌ Cannot CLOSE: portfolio is {portfolio.status}"
    payload = {
        "requested_at": sender.sent_at.isoformat(),
        "sender": {"kind": sender.kind, "user_id": sender.user_id, "message_id": sender.message_id},
    }
    fsutil.atomic_write_text(_pending_close_path(ctx.store), json.dumps(payload, indent=2) + "\n")
    return "✅ Close requested. Send CLOSE CONFIRM within 10 minutes to run the final valuation and close."


def cmd_close_confirm(ctx: CommandContext, sender: Sender) -> str:
    """Confirm a close: must follow ``CLOSE`` from the same authority within 10 minutes (TS CMD-C10)."""
    path = _pending_close_path(ctx.store)
    if not path.exists():
        return "❌ No pending CLOSE request"
    payload = json.loads(path.read_text(encoding="utf-8"))
    requested_at = datetime.fromisoformat(payload["requested_at"])
    requester = payload["sender"]
    if (requester["kind"], requester["user_id"]) != (sender.kind, sender.user_id):
        return "❌ CLOSE CONFIRM must come from the same sender that requested CLOSE"
    if sender.sent_at - requested_at > CLOSE_CONFIRM_WINDOW:
        return "❌ CLOSE request expired; send CLOSE again"
    engine = ExecutionEngine(
        ctx.store, ctx.clock, ctx.calendar, ctx.price_provider, ctx.price_fallback or ctx.price_provider
    )
    result = engine.run_final_valuation(benchmark=ctx.cfg.benchmark)
    if result.outcome == "failed_closed":
        return f"❌ Cannot price the portfolio, so nothing was closed. Send CLOSE CONFIRM again to retry. ({result.detail})"
    if result.outcome == "lock_busy":
        return "❌ System busy"
    if result.outcome == "skipped_not_live":
        return f"❌ Cannot close: {result.detail}"
    path.unlink(missing_ok=True)
    return "✅ Closed. The final report is recorded; positions are kept, not sold."


# -- RECONFIRM -------------------------------------------------------------------------------------------------


def _next_seq(store: FileStore, day: date) -> int:
    prefix = f"P-{day.isoformat()}-"
    directory = store.root / "proposals"
    if not directory.exists():
        return 1
    used = [int(p.stem[len(prefix) :]) for p in directory.glob(f"{prefix}*.json") if p.stem[len(prefix) :].isdigit()]
    return max(used, default=0) + 1


def cmd_reconfirm(ctx: CommandContext, proposal_id: str | None, sender: Sender) -> str:
    """Reconfirm an ``EXPIRED`` proposal (AS §8.7, TS CMD-RC01..06).

    Creates the replacement immediately so the reply can carry its ID: ``explicit`` mode whatever the config says,
    ``reconfirms`` pointing at the original, ``reconfirm_check == "pending"``. The original gets ``reconfirmed_as``
    in the same transaction, which is what makes a second ``RECONFIRM`` a no-op. Fresh pricing, the cap re-check
    and the thesis re-check are the next tick's ``reconfirm:<id>`` job (``evaluation/reconfirm.py``), never this
    handler: a slow network or LLM call must not sit inside a chat reply.
    """
    if proposal_id is None or not PROPOSAL_ID_RE.fullmatch(proposal_id):
        return "❌ RECONFIRM needs a valid proposal ID, e.g. RECONFIRM P-2026-10-24-01"
    now = sender.sent_at.astimezone(PT)
    try:
        with ctx.store.transaction("cmd:reconfirm") as txn:
            txn.require(status="LIVE")
            txn.reconcile()
            doc = _load_proposal(ctx.store, proposal_id)
            if doc is None:
                return "❌ Unknown or expired proposal ID"
            if doc.state != "EXPIRED":
                return f"❌ Not expired: {proposal_id} is {doc.state}"
            if doc.reconfirmed_as is not None:
                return f"❌ {proposal_id} was already reconfirmed as {doc.reconfirmed_as}"
            new_pid = domain_ids.proposal_id(now.date(), _next_seq(ctx.store, now.date()))
            execute_at = ctx.calendar.next_session_open_at_or_after(
                add_elapsed(now, timedelta(hours=doc.veto_window_hours))
            )
            first = _genesis_transition(now, "system:reconfirm", f"reconfirm of {proposal_id} requested by {_actor(sender)}")
            new_doc = doc.model_copy(
                update={
                    "proposal_id": new_pid,
                    "created_at": now,
                    "state": "PROPOSED",
                    "approval_mode": "explicit",  # never auto, whatever auto_approve says (TS EDGE-23)
                    "execute_at": execute_at,
                    "expires_at": execute_at + timedelta(minutes=ctx.cfg.proposal_grace_minutes),
                    "fill_session_date": execute_at.date(),
                    "legs": [leg.model_copy(update={"trade_id": domain_ids.trade_id(new_pid, leg.leg)}) for leg in doc.legs],
                    "veto_deadline": None,
                    "reconfirms": proposal_id,
                    "reconfirmed_as": None,
                    "reconfirm_check": "pending",
                    "transitions": [first],
                }
            )
            txn.stage_proposals({new_pid: new_doc, proposal_id: doc.model_copy(update={"reconfirmed_as": new_pid})})
    except GateError as exc:
        return f"❌ {exc}"
    except ReconcileHalt:
        return "❌ System halted"
    except LockBusy:
        return "❌ System busy"
    return f"✅ Reconfirm accepted: {proposal_id} -> {new_pid}. It is re-priced and re-checked on the next tick, then needs your APPROVE."


# -- RECONCILE -------------------------------------------------------------------------------------------------


def cmd_reconcile(ctx: CommandContext, mode: str, sender: Sender) -> str:
    """Reconcile the books. Read-only by default; ``rebuild`` rewrites ``trades.log`` from ``portfolio.json``."""
    try:
        with ctx.store.transaction("cmd:reconcile") as txn:
            try:
                txn.reconcile()
            except ReconcileHalt as exc:
                if mode != "rebuild":
                    return "❌ Books diverge: " + "; ".join(exc.divergences)
                lines = [format_log_line(t) for t in txn.state.trades]
                fsutil.atomic_write_text(ctx.store.trades_log_path, "".join(line + "\n" for line in lines))
                return "✅ trades.log rebuilt from portfolio.json."
    except LockBusy:
        return "❌ System busy"
    return "✅ Books reconcile cleanly."


# -- STATUS ----------------------------------------------------------------------------------------------------


def cmd_status(ctx: CommandContext, sender: Sender) -> str:
    """Lock-free, read-only status (TS CMD-ST05): never goes through ``Transaction``."""
    if not ctx.store.portfolio_exists():
        return "✅ STATUS: no portfolio yet."
    portfolio = ctx.store.load_portfolio()
    pending = [p for p in _load_all_proposals(ctx.store) if not is_terminal(p.state)]
    lock_meta = ctx.store.lock.read_meta()
    lines = [
        f"✅ STATUS: {portfolio.status}",
        f"cash: {portfolio.cash}",
        f"positions: {len(portfolio.positions)}",
        f"lock: {'held by ' + lock_meta.job if lock_meta else 'free'}",
    ]
    if pending:
        lines.append("pending proposals:")
        lines += [f"  {p.proposal_id} state={p.state} execute_at={p.execute_at.isoformat()}" for p in pending]
    else:
        lines.append("pending proposals: none")
    return "\n".join(lines)


# -- SET ---------------------------------------------------------------------------------------------------------


def cmd_set(ctx: CommandContext, key: str | None, value: str | None, sender: Sender) -> str:
    """``SET <key> <value>``: only keys in ``SETTABLE_KEYS`` (plus ``start_cash`` while ``STAGED``), AS §3.6."""
    if not key or value is None:
        return "❌ SET needs a key and a value, e.g. SET max_position_pct 25"
    if key.startswith(BLOCKED_SET_PREFIXES):
        return f"❌ {key} is file-edit only and cannot be changed with SET"
    if key != "start_cash" and key not in SETTABLE_KEYS:
        return f"❌ {key} cannot be changed with SET"
    try:
        with ctx.store.transaction("cmd:set") as txn:
            if key == "start_cash":
                txn.require(status="STAGED")
            dumped = ctx.cfg.model_dump(mode="json")
            dumped[key] = value
            try:
                new_cfg = Config.model_validate(dumped)
            except ValidationError as exc:
                return f"❌ Invalid value for {key}: {exc.errors()[0]['msg']}"
            ctx.cfg = new_cfg
    except GateError as exc:
        return f"❌ {exc}"
    except LockBusy:
        return "❌ System busy"
    return f"✅ Set {key} = {value}"
