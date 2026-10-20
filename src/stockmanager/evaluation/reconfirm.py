"""The ``reconfirm:<proposal_id>`` job (AS §8.7): validates a ``RECONFIRM`` replacement on the next tick.

``cmd_reconfirm`` creates the replacement at once (``PROPOSED``, ``reconfirm_check == "pending"``) so the reply can
carry its ID. This job then does the slow part, outside the reply path:

1. a fresh single-source valuation (``PriceService``; fail closed, so a price failure leaves it pending and the next
   tick retries),
2. the thesis re-check (``ThesisReview``, lock-free because it may call an LLM),
3. under the lock, the cap re-check: the stored legs go back through ``ProposalBuilder`` as intent, so the same
   resize-or-drop rules as a normal evaluation apply (TS EDGE-22).

If all pass, the replacement gets the rebuilt legs, a fresh ``execute_at``/``expires_at`` (no clock runs before this
point), moves to ``VETO_WINDOW`` and the ``veto_window_open`` alert is queued. It stays ``explicit``: it needs the
user's ``APPROVE`` whatever ``auto_approve`` says (TS EDGE-23). Otherwise it ends ``EXPIRED`` with an alert that says
why and carries ``RECONFIRM <id>`` again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, Protocol

from stockmanager.config.model import Config
from stockmanager.config.proposals import ProposalConfig
from stockmanager.domain import ids as domain_ids
from stockmanager.domain.docs import PortfolioDoc, PriceTable, ProposalDoc, TransitionRecord
from stockmanager.domain.ports import PriceProvider, SessionCalendar, SymbolVerifier
from stockmanager.domain.proposal_state import ProposalState, chain_hash
from stockmanager.domain.timeutil import add_elapsed
from stockmanager.evaluation.builder import PT, ProposalBuilder
from stockmanager.evaluation.reasoner_io import (
    LimitsCheck,
    ReasonerOutput,
    RiskAssessment,
    SizeIntent,
    TradeIdea,
    TradeLeg,
)
from stockmanager.notify.outbox import Alert, prepare
from stockmanager.notify.templates import proposal_expired, veto_window_open
from stockmanager.pricing.errors import ValuationUnavailable
from stockmanager.pricing.service import PriceService
from stockmanager.store import FileStore
from stockmanager.store.lock import Clock, LockBusy
from stockmanager.store.transaction import GateError, ReconcileHalt

#: A replacement whose prices stay unavailable this long is given up on (it holds no clock, so nothing else would).
MAX_PENDING = timedelta(hours=72)

Outcome = Literal["passed", "voided", "pending", "no_op", "lock_busy", "halted_reconcile", "aborted_state_changed"]


@dataclass(frozen=True)
class ThesisVerdict:
    """The thesis re-check's answer: keep the proposal, or void it with a reason for the user."""

    action: Literal["keep", "void"]
    reason: str = ""


class ThesisReview(Protocol):
    """Re-checks a replacement's thesis against the stored facts and the fresh ``valuation`` (AS §8.7).

    May be slow (an LLM call), so the job calls it with no lock held. Resizing is not its concern: the cap re-check
    already resizes. ``evaluation.thesis_checker.ThesisReview`` is the keyword implementation wired in production;
    LP Q16 leaves an LLM version unspecified.
    """

    def check(self, doc: ProposalDoc, valuation: PriceTable) -> ThesisVerdict:
        """Return ``keep`` or ``void`` for ``doc``."""
        ...


@dataclass(frozen=True)
class ReconfirmResult:
    """What one run of the job did. ``detail`` is the user-facing reason for ``voided``, else diagnostics."""

    outcome: Outcome
    detail: str = ""


def _transition(doc: ProposalDoc, to_state: ProposalState, cause: str, at: datetime) -> ProposalDoc:
    prev = doc.transitions[-1]
    rec = TransitionRecord(
        seq=prev.seq + 1,
        from_state=doc.state,
        to=to_state,
        at=at,
        actor="system:reconfirm",
        cause=cause,
        prev_hash=prev.hash,
        hash="",
    )
    rec = rec.model_copy(update={"hash": chain_hash(prev.hash, rec.model_dump(mode="json", by_alias=True))})
    return doc.model_copy(update={"state": to_state, "transitions": [*doc.transitions, rec]})


def _intent_of(doc: ProposalDoc) -> ReasonerOutput:
    """The stored legs as Reasoner intent, so ``ProposalBuilder`` can re-validate and re-size them."""
    legs = [
        TradeLeg(
            leg=leg.leg,
            action=leg.action,
            ticker=leg.ticker,
            thesis=leg.thesis or None,
            size_intent=SizeIntent(
                kind=leg.size.kind,
                fraction=None if leg.size.fraction is None else str(leg.size.fraction),
                leg=leg.size.source_leg,
                value=None if leg.size.target_value is None else str(leg.size.target_value),
            ),
        )
        for leg in doc.legs
    ]
    # The self-assessment is audit-only and unused by the builder (LP §4.6); this one just satisfies the contract.
    risk = RiskAssessment(
        sector="",
        largest_position_after_pct="0",
        sector_weight_after_pct="0",
        main_risks=[],
        invalidation="",
        confidence="low",
        limits_check=LimitsCheck(
            position_cap=True, new_position_cap=True, sector_cap=True, trade_cap=True, long_only=True, cash=True
        ),
    )
    idea = TradeIdea(
        priority=1,
        rationale=doc.rationale,
        why_beats_hold="",
        legs=legs,
        risk_assessment=risk,
        evidence_fact_ids=[],
    )
    return ReasonerOutput(summary="", hold_alternative=doc.hold_alternative, holdings=[], ideas=[idea])


def _valuation_date(now: datetime) -> date:
    """The latest weekday on or before today in PT. Holidays fail closed at the provider and retry."""
    day = now.astimezone(PT).date()
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


class ReconfirmEngine:
    """``run(proposal_id, cfg)``: validate one pending ``RECONFIRM`` replacement."""

    def __init__(
        self,
        store: FileStore,
        clock: Clock,
        calendar: SessionCalendar,
        verifier: SymbolVerifier,
        price_primary: PriceProvider,
        price_fallback: PriceProvider,
        thesis_checker: ThesisReview | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._builder = ProposalBuilder(calendar, verifier)
        self._primary = price_primary
        self._fallback = price_fallback
        self._thesis = thesis_checker
        self._prices = PriceService()

    def _load_pending(self, proposal_id: str) -> ProposalDoc | None:
        path = self._store.proposal_path(proposal_id)
        if not path.exists():
            return None
        doc = ProposalDoc.model_validate_json(path.read_text(encoding="utf-8"))
        return doc if doc.state == "PROPOSED" and doc.reconfirm_check == "pending" else None

    def run(self, proposal_id: str, cfg: Config) -> ReconfirmResult:
        """Validate ``proposal_id`` (AS §8.7). Idempotent: anything not ``PROPOSED``/pending is a no-op."""
        doc = self._load_pending(proposal_id)
        if doc is None or not self._store.portfolio_exists():
            return ReconfirmResult("no_op")
        snapshot = self._store.load_portfolio()
        if snapshot.status != "LIVE":
            return ReconfirmResult("no_op", f"portfolio is {snapshot.status}")

        # No lock: prices and the thesis check can be slow.
        tickers = sorted({p.ticker for p in snapshot.positions} | {leg.ticker for leg in doc.legs})
        try:
            valuation = self._prices.valuation(tickers, _valuation_date(self._clock()), self._primary, self._fallback)
        except ValuationUnavailable as exc:
            if self._clock() - doc.created_at <= MAX_PENDING:
                return ReconfirmResult("pending", str(exc))  # fail closed; the next tick retries
            return self._void(proposal_id, "prices stayed unavailable, so it could not be re-checked")
        if self._thesis is not None:
            verdict = self._thesis.check(doc, valuation)
            if verdict.action == "void":
                return self._void(proposal_id, f"the thesis no longer holds: {verdict.reason}")
        return self._commit(proposal_id, cfg, snapshot, valuation)

    def _commit(self, proposal_id: str, cfg: Config, snapshot: PortfolioDoc, valuation: PriceTable) -> ReconfirmResult:
        proposal_cfg = ProposalConfig(
            max_position_pct=cfg.max_position_pct,
            max_trades_per_cycle=cfg.max_trades_per_cycle,
            auto_approve=cfg.auto_approve,
            veto_window_hours=cfg.veto_window_hours,
            proposal_grace_minutes=cfg.proposal_grace_minutes,
        )
        try:
            with self._store.transaction("reconfirm") as txn:
                txn.require(status="LIVE")
                txn.reconcile()
                doc = self._load_pending(proposal_id)
                if doc is None:
                    return ReconfirmResult("no_op", "no longer pending")
                if txn.state.rev != snapshot.rev:
                    return ReconfirmResult("aborted_state_changed", "rev changed since the valuation; the tick retries")
                now = self._clock()
                result = self._builder.build(_intent_of(doc), txn.state, valuation, proposal_cfg, self._clock)
                if not result.proposals:
                    reasons = ", ".join(f"{n.reason} ({n.detail})" if n.detail else n.reason for n in result.not_proposed)
                    done, alert = self._voided(doc, f"it no longer fits the limits: {reasons}", now)
                    txn.stage_proposals({proposal_id: done})
                    txn.stage_alerts({f"{alert.alert_id}.json": alert.to_json()})
                    return ReconfirmResult("voided", reasons)
                built = result.proposals[0]
                passed = doc.model_copy(
                    update={
                        "veto_window_hours": built.veto_window_hours,
                        "veto_deadline": add_elapsed(now, timedelta(hours=built.veto_window_hours)),
                        "execute_at": built.execute_at,
                        "expires_at": built.expires_at,
                        "fill_session_date": built.fill_session_date,
                        "caps": built.caps,
                        "legs": [
                            leg.model_copy(update={"trade_id": domain_ids.trade_id(doc.proposal_id, leg.leg)})
                            for leg in built.legs
                        ],
                        "reconfirm_check": "passed",
                    }
                )
                cause = "reconfirm_passed" if self._thesis else "reconfirm_passed; thesis re-check not configured"
                passed = _transition(passed, "VETO_WINDOW", cause, now)
                alert = prepare(veto_window_open(proposal_id, passed.veto_window_hours), now)
                txn.stage_proposals({proposal_id: passed})
                txn.stage_alerts({f"{alert.alert_id}.json": alert.to_json()})
        except GateError as exc:
            return ReconfirmResult("no_op", str(exc))
        except ReconcileHalt as exc:
            return ReconfirmResult("halted_reconcile", "; ".join(exc.divergences))
        except LockBusy:
            return ReconfirmResult("lock_busy")
        return ReconfirmResult("passed")

    def _voided(self, doc: ProposalDoc, reason: str, now: datetime) -> tuple[ProposalDoc, Alert]:
        ended = doc.model_copy(update={"reconfirm_check": "failed"})
        ended = _transition(ended, "EXPIRED", f"reconfirm_failed: {reason}"[:200], now)
        return ended, prepare(proposal_expired(doc.proposal_id, f"Reconfirm of {doc.reconfirms} failed: {reason}."), now)

    def _void(self, proposal_id: str, reason: str) -> ReconfirmResult:
        """End a still-pending replacement (thesis voided it, or prices never came back)."""
        try:
            with self._store.transaction("reconfirm") as txn:
                doc = self._load_pending(proposal_id)
                if doc is None:
                    return ReconfirmResult("no_op", "no longer pending")
                done, alert = self._voided(doc, reason, self._clock())
                txn.stage_proposals({proposal_id: done})
                txn.stage_alerts({f"{alert.alert_id}.json": alert.to_json()})
        except LockBusy:
            return ReconfirmResult("lock_busy")
        return ReconfirmResult("voided", reason)


__all__ = ["MAX_PENDING", "ReconfirmEngine", "ReconfirmResult", "ThesisReview", "ThesisVerdict"]
