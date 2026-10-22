"""Evaluation engine (AS §1.2): phases A (snapshot) -> B (gather & think) -> C (commit) -> D (report).

Phase B runs lock-free (prices, news, the Reasoner call) so a slow network or LLM call never blocks ``VETO``;
the lock is held only to commit, with an optimistic ``rev`` check guarding against a state change during phase B
(⚑AD1). ``ProposalBuilder`` runs inside phase C against freshly re-read config and books.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from stockmanager.config.model import Config
from stockmanager.config.proposals import ProposalConfig
from stockmanager.dashboard.build import build_dashboard
from stockmanager.dashboard.render import render_dashboard
from stockmanager.dashboard.write import write_dashboard
from stockmanager.domain.docs import PortfolioDoc, PriceTable, ProposalDoc
from stockmanager.domain.ports import PriceProvider, SessionCalendar, SymbolVerifier
from stockmanager.domain.proposal_state import is_terminal
from stockmanager.domain.sessions import last_completed_session
from stockmanager.domain.timeutil import PT
from stockmanager.evaluation.builder import ProposalBuilder
from stockmanager.evaluation.reasoner import Reasoner, ReasonerFailure
from stockmanager.evaluation.reasoner_io import ReasonerInput, ReasonerOutput
from stockmanager.news import NewsResult
from stockmanager.notify.emit import emit_reconcile_divergence, emit_run_skipped_busy
from stockmanager.notify.outbox import prepare, raised_dedupe_keys
from stockmanager.notify.templates import evaluation_complete, news_unavailable, veto_window_open
from stockmanager.pricing.errors import ValuationUnavailable
from stockmanager.pricing.service import PriceService
from stockmanager.scheduler.scheduler import ScheduleSlot
from stockmanager.store import FileStore
from stockmanager.store.lock import Clock, LockBusy
from stockmanager.store.transaction import GateError, ReconcileHalt, Transaction

Outcome = Literal[
    "completed",
    "skipped_paused",
    "skipped_not_live",
    "halted_reconcile",
    "failed_closed",
    "lock_busy",
    "aborted_state_changed",
]


#: At most this many minor divergences are logged and tolerated; more than this halts.
MAX_MINOR_DIVERGENCES = 3
#: A share-count difference up to this size is "minor"; anything else is critical.
MINOR_SHARE_DELTA = Decimal("1")
_MINOR_RE = re.compile(r"^[^:]+: shares stored (?P<stored>\d+\.\d+), derived (?P<derived>\d+\.\d+)$")

_log = logging.getLogger(__name__)


def _is_minor(divergence: str) -> bool:
    """True for a small share-count difference on a position that is open on both sides.

    Cash, cost basis, missing or extra positions, a zero-share position that should have been closed and every
    ``trades.log`` mismatch are critical.
    """
    m = _MINOR_RE.match(divergence)
    if m is None:
        return False
    stored, derived = Decimal(m["stored"]), Decimal(m["derived"])
    return stored > 0 and derived > 0 and abs(stored - derived) <= MINOR_SHARE_DELTA


def _reconcile_tolerant(txn: Transaction) -> None:
    """Reconcile, warning and carrying on when there are <= 3 divergences and all are minor; otherwise re-raise."""
    try:
        txn.reconcile()
    except ReconcileHalt as exc:
        if len(exc.divergences) > MAX_MINOR_DIVERGENCES or not all(_is_minor(d) for d in exc.divergences):
            raise
        for d in exc.divergences:
            _log.warning("Reconcile: minor divergence, continuing: %s", d)


@dataclass(frozen=True)
class RunResult:
    """What one evaluation run did (AS §1.2). ``detail`` carries a Reasoner failure reason on a quiet cycle."""

    outcome: Outcome
    detail: str = ""


#: Runs the news stage for ``cycle_id`` against ``snapshot`` (persists ``extracted-facts/*`` itself, AS §7).
NewsRunner = Callable[[str, PortfolioDoc], NewsResult]

#: Projects the cycle's evidence into a ``ReasonerInput`` (AS §7.3): cycle id, snapshot, valuation, news, config.
ReasonerInputBuilder = Callable[[str, PortfolioDoc, PriceTable, NewsResult, Config], ReasonerInput]


def _unannounced(proposals: list[ProposalDoc], cycle_date: date, announced: set[str]) -> list[ProposalDoc]:
    """Open proposals an earlier run of this cycle created whose ``veto_window_open`` alert was never queued.

    That is a run that died between committing the proposal file and the alerts (AS §8.5 steps 3 and 7): the owner
    was never told, so a retry replaces the proposal instead of leaving a second one beside it (TS NN-29, AC-02).
    """
    return [
        p
        for p in proposals
        if not is_terminal(p.state)
        and p.transitions[0].actor == "system:evaluation"
        and p.created_at.astimezone(PT).date() == cycle_date
        and f"veto_window_open:{p.proposal_id}" not in announced
    ]


class EvaluationEngine:
    """``run(slot, cfg)`` -- one dated evaluation cycle (AS §1.2)."""

    def __init__(
        self,
        store: FileStore,
        clock: Clock,
        calendar: SessionCalendar,
        verifier: SymbolVerifier,
        price_primary: PriceProvider,
        price_fallback: PriceProvider,
        reasoner: Reasoner,
        news_runner: NewsRunner,
        build_reasoner_input: ReasonerInputBuilder,
    ) -> None:
        self._store = store
        self._clock = clock
        self._calendar = calendar
        self._verifier = verifier
        self._primary = price_primary
        self._fallback = price_fallback
        self._reasoner = reasoner
        self._news_runner = news_runner
        self._build_input = build_reasoner_input
        self._prices = PriceService()

    def run(self, slot: ScheduleSlot, cfg: Config) -> RunResult:
        """Run one cycle for ``slot`` under ``cfg`` (AS §1.2 phases A-D)."""
        # Phase A: snapshot, no lock.
        if not self._store.portfolio_exists():
            return RunResult("skipped_not_live", "no portfolio")
        snapshot = self._store.load_portfolio()
        if snapshot.status == "PAUSED":
            return RunResult("skipped_paused")
        if snapshot.status != "LIVE":
            return RunResult("skipped_not_live", snapshot.status)
        try:
            with self._store.transaction("evaluation:precheck", max_attempts=1, sleep_seconds=0) as txn:
                _reconcile_tolerant(txn)
        except ReconcileHalt as exc:
            emit_reconcile_divergence(self._store, exc.divergences)
            return RunResult("halted_reconcile", "; ".join(exc.divergences))
        except LockBusy:
            emit_run_skipped_busy(self._store, "evaluation", slot.cycle_id)
            return RunResult("lock_busy")
        rev_before = snapshot.rev

        # Phase B: gather and think, no lock.
        cycle_date = date.fromisoformat(slot.cycle_id)
        # Prices are the last completed session's close: a Saturday cycle has no bar of its own (TS EDGE-01/EDGE-14).
        valuation_date = last_completed_session(self._calendar, slot.slot_time)
        tickers = sorted({p.ticker for p in snapshot.positions} | {cfg.benchmark})
        try:
            valuation = self._prices.valuation(tickers, valuation_date, self._primary, self._fallback)
        except ValuationUnavailable as exc:
            return RunResult("failed_closed", str(exc))

        news = self._news_runner(slot.cycle_id, snapshot)
        builder = ProposalBuilder(self._calendar, self._verifier)
        failure_reason = ""
        if news.news_status == "unavailable":
            # No evidence, no proposals (TS EDGE-09 / D14): the Reasoner is not asked, the report still goes out.
            intent = ReasonerOutput(summary="", hold_alternative="", holdings=[], ideas=[])
            failure_reason = "news_unavailable"
        else:
            reasoner_input = self._build_input(slot.cycle_id, snapshot, valuation, news, cfg)
            try:
                intent = self._reasoner.evaluate(reasoner_input)
            except ReasonerFailure as exc:
                intent = ReasonerOutput(summary="", hold_alternative="", holdings=[], ideas=[])
                failure_reason = exc.reason
            # A buy of a name not yet held can only be priced once the Reasoner has named it. Re-value with it
            # included so the table stays one source (SS §6); if that fails the holdings table stands and the
            # builder drops the idea as ``unpriced`` rather than the whole cycle failing on one bad ticker.
            new_names = builder.unheld_buy_tickers(intent, snapshot)
            if new_names:
                try:
                    valuation = self._prices.valuation(
                        sorted({*tickers, *new_names}), valuation_date, self._primary, self._fallback
                    )
                except ValuationUnavailable:
                    pass

        # Phase C: commit, under the lock.
        proposal_cfg = ProposalConfig(
            max_position_pct=cfg.max_position_pct,
            max_trades_per_cycle=cfg.max_trades_per_cycle,
            auto_approve=cfg.auto_approve,
            veto_window_hours=cfg.veto_window_hours,
            proposal_grace_minutes=cfg.proposal_grace_minutes,
        )
        try:
            with self._store.transaction("evaluation") as txn:
                txn.require(status="LIVE")
                _reconcile_tolerant(txn)
                if txn.state.rev != rev_before:
                    return RunResult("aborted_state_changed", "rev changed since phase A; the tick retries")
                now = self._clock()
                on_disk = self._store.load_proposals()
                orphans = {p.proposal_id: p for p in _unannounced(on_disk, cycle_date, raised_dedupe_keys(self._store.root))}
                proposals_dir = self._store.root / "proposals"
                file_ids = [p.stem for p in proposals_dir.glob("*.json")] if proposals_dir.exists() else []
                existing_ids = [i for i in file_ids if i not in orphans]  # an orphan's id is free to be overwritten
                result = builder.build(intent, txn.state, valuation, proposal_cfg, self._clock, existing_ids)
                staged = {p.proposal_id: p for p in result.proposals}
                for pid, orphan in orphans.items():
                    if pid not in staged:  # nothing took over its id: close it out, the file cannot be deleted
                        staged[pid] = orphan.transition(
                            "VETOED", "system:evaluation", f"superseded: cycle {slot.cycle_id} re-ran", now
                        )
                if staged:
                    txn.stage_proposals(staged)

                # Phase D: report -- alerts are enqueued in the same commit (AS §1.2 step 8).
                alerts: dict[str, str] = {}
                summary = prepare(evaluation_complete(f"eval-{slot.cycle_id}", cycle_date, slot.late), now)
                alerts[f"{summary.alert_id}.json"] = summary.to_json()
                for p in result.proposals:
                    a = prepare(veto_window_open(p.proposal_id, p.veto_window_hours), now)
                    alerts[f"{a.alert_id}.json"] = a.to_json()
                if news.news_status == "unavailable":
                    a2 = prepare(news_unavailable(cycle_date), now)
                    alerts[f"{a2.alert_id}.json"] = a2.to_json()
                txn.stage_alerts(alerts)
        except LockBusy:
            emit_run_skipped_busy(self._store, "evaluation", slot.cycle_id)
            return RunResult("lock_busy")
        except ReconcileHalt as exc:
            emit_reconcile_divergence(self._store, exc.divergences)
            return RunResult("halted_reconcile", "; ".join(exc.divergences))
        except GateError as exc:
            return RunResult("skipped_not_live", str(exc))

        # Build and write dashboard post-commit so it captures the updated state.
        try:
            model = build_dashboard(self._store, self._clock)
            html = render_dashboard(model)
            write_dashboard(model, self._store.root, render_dashboard, {})
        except Exception:
            logging.getLogger(__name__).exception("Dashboard build failed")
        return RunResult("completed", failure_reason)
