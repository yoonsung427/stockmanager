"""Evaluation engine (AS §1.2): phases A (snapshot) -> B (gather & think) -> C (commit) -> D (report).

Phase B runs lock-free (prices, news, the Reasoner call) so a slow network or LLM call never blocks ``VETO``;
the lock is held only to commit, with an optimistic ``rev`` check guarding against a state change during phase B
(⚑AD1). ``ProposalBuilder`` runs inside phase C against freshly re-read config and books.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from stockmanager.config.model import Config
from stockmanager.config.proposals import ProposalConfig
from stockmanager.domain.docs import PortfolioDoc, PriceTable
from stockmanager.domain.ports import PriceProvider, SessionCalendar, SymbolVerifier
from stockmanager.evaluation.builder import ProposalBuilder
from stockmanager.evaluation.reasoner import Reasoner, ReasonerFailure
from stockmanager.evaluation.reasoner_io import ReasonerInput, ReasonerOutput
from stockmanager.news import NewsResult
from stockmanager.notify.outbox import prepare
from stockmanager.notify.templates import evaluation_complete, news_unavailable, veto_window_open
from stockmanager.pricing.errors import ValuationUnavailable
from stockmanager.pricing.service import PriceService
from stockmanager.scheduler.scheduler import ScheduleSlot
from stockmanager.store import FileStore
from stockmanager.store.lock import Clock, LockBusy
from stockmanager.store.transaction import GateError, ReconcileHalt

Outcome = Literal[
    "completed",
    "skipped_paused",
    "skipped_not_live",
    "halted_reconcile",
    "failed_closed",
    "lock_busy",
    "aborted_state_changed",
]


@dataclass(frozen=True)
class RunResult:
    """What one evaluation run did (AS §1.2). ``detail`` carries a Reasoner failure reason on a quiet cycle."""

    outcome: Outcome
    detail: str = ""


#: Runs the news stage for ``cycle_id`` against ``snapshot`` (persists ``extracted-facts/*`` itself, AS §7).
NewsRunner = Callable[[str, PortfolioDoc], NewsResult]

#: Projects the cycle's evidence into a ``ReasonerInput`` (AS §7.3): cycle id, snapshot, valuation, news, config.
ReasonerInputBuilder = Callable[[str, PortfolioDoc, PriceTable, NewsResult, Config], ReasonerInput]


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
                txn.reconcile()
        except ReconcileHalt as exc:
            return RunResult("halted_reconcile", "; ".join(exc.divergences))
        except LockBusy:
            return RunResult("lock_busy")
        rev_before = snapshot.rev

        # Phase B: gather and think, no lock.
        valuation_date = date.fromisoformat(slot.cycle_id)
        tickers = sorted({p.ticker for p in snapshot.positions} | {cfg.benchmark})
        try:
            valuation = self._prices.valuation(tickers, valuation_date, self._primary, self._fallback)
        except ValuationUnavailable as exc:
            return RunResult("failed_closed", str(exc))

        news = self._news_runner(slot.cycle_id, snapshot)
        reasoner_input = self._build_input(slot.cycle_id, snapshot, valuation, news, cfg)
        failure_reason = ""
        try:
            intent: ReasonerOutput = self._reasoner.evaluate(reasoner_input)
        except ReasonerFailure as exc:
            intent = ReasonerOutput(summary="", hold_alternative="", holdings=[], ideas=[])
            failure_reason = exc.reason

        # Phase C: commit, under the lock.
        proposal_cfg = ProposalConfig(
            max_position_pct=cfg.max_position_pct,
            max_trades_per_cycle=cfg.max_trades_per_cycle,
            auto_approve=cfg.auto_approve,
            veto_window_hours=cfg.veto_window_hours,
            proposal_grace_minutes=cfg.proposal_grace_minutes,
        )
        builder = ProposalBuilder(self._calendar, self._verifier)
        try:
            with self._store.transaction("evaluation") as txn:
                txn.require(status="LIVE")
                txn.reconcile()
                if txn.state.rev != rev_before:
                    return RunResult("aborted_state_changed", "rev changed since phase A; the tick retries")
                proposals_dir = self._store.root / "proposals"
                existing_ids = [p.stem for p in proposals_dir.glob("*.json")] if proposals_dir.exists() else []
                result = builder.build(intent, txn.state, valuation, proposal_cfg, self._clock, existing_ids)
                if result.proposals:
                    txn.stage_proposals({p.proposal_id: p for p in result.proposals})

                # Phase D: report -- alerts are enqueued in the same commit (AS §1.2 step 8).
                now = self._clock()
                alerts: dict[str, str] = {}
                summary = prepare(evaluation_complete(f"eval-{slot.cycle_id}", valuation_date, slot.late), now)
                alerts[f"{summary.alert_id}.json"] = summary.to_json()
                for p in result.proposals:
                    a = prepare(veto_window_open(p.proposal_id, p.veto_window_hours), now)
                    alerts[f"{a.alert_id}.json"] = a.to_json()
                if news.news_status == "unavailable":
                    a2 = prepare(news_unavailable(valuation_date), now)
                    alerts[f"{a2.alert_id}.json"] = a2.to_json()
                txn.stage_alerts(alerts)
        except LockBusy:
            return RunResult("lock_busy")
        except GateError as exc:
            return RunResult("skipped_not_live", str(exc))
        return RunResult("completed", failure_reason)
