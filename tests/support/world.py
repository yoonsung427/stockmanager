"""``World``: the TS §0.3 seams wired around one temp workspace, plus the standard fixtures of TS §2 (S-LIVE, PR-PAIR...)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from stockmanager.domain.docs import Fact, PortfolioDoc, ProposalDoc, ProposalLeg, ProposalSize, Trade
from stockmanager.evaluation.engine import EvaluationEngine, NewsRunner
from stockmanager.evaluation.input_builder import make_reasoner_input_builder
from stockmanager.evaluation.reasoner import Reasoner
from stockmanager.execution.engine import ExecutionEngine
from stockmanager.news import NewsResult, NewsStats
from stockmanager.scheduler.scheduler import ScheduleSlot
from stockmanager.sim import (
    FakeCalendar,
    FakeClock,
    FakePidChecker,
    FakeSymbolVerifier,
    FaultInjector,
    ScriptedPriceProvider,
)
from stockmanager.sim.clock import PT
from stockmanager.sim.prices import Script
from stockmanager.store import FileStore
from stockmanager.store.reconcile import format_log_line
from tests.support.books import make_portfolio, make_trade
from tests.support.proposals import advance, make_proposal

EVAL_SAT = datetime(2026, 10, 24, 9, 0, tzinfo=PT)
WIN_CLOSE = datetime(2026, 10, 24, 13, 0, tzinfo=PT)
SESSION_OPEN = datetime(2026, 10, 26, 6, 30, tzinfo=PT)
EXEC = datetime(2026, 10, 26, 6, 35, tzinfo=PT)
EXPIRES = datetime(2026, 10, 26, 7, 30, tzinfo=PT)

#: P-BASE: one price per ticker, identical on both providers unless a test changes one.
P_BASE: dict[str, Decimal] = {
    "AAPL": Decimal(200),
    "MSFT": Decimal(400),
    "JPM": Decimal(100),
    "SPY": Decimal(500),
}

EVAL_SLOT = ScheduleSlot(slot_time=EVAL_SAT, cycle_id="2026-10-24", late=False)

PAIR_ID = "P-2026-10-24-01"
SINGLE_ID = "P-2026-10-24-02"


class World:
    """Temp workspace + fake clock + scripted providers. ``restart`` simulates a fresh process on the same files."""

    def __init__(self, root: Path, now: datetime = EXEC, holidays: Iterable[date] = ()) -> None:
        self.root = root
        self.clock = FakeClock(now)
        self.pids = FakePidChecker()
        self.calendar = FakeCalendar(holidays)
        self.yahoo = ScriptedPriceProvider("yahoo", P_BASE, self.clock)
        self.finnhub = ScriptedPriceProvider("finnhub", P_BASE, self.clock)
        self.store = self._new_store()

    def _new_store(self) -> FileStore:
        return FileStore(self.root, clock=self.clock, pid_checker=self.pids, sleep=lambda _s: None)

    def restart(self, minutes: float = 1) -> FileStore:
        """A fresh process: advance the clock, build a new ``FileStore`` (no injected faults, no memory of the old one)."""
        self.clock.advance(minutes=minutes)
        self.store = self._new_store()
        return self.store

    def injector(self) -> FaultInjector:
        """A fault injector bound to the current store."""
        return FaultInjector(self.store)

    def engine(self) -> ExecutionEngine:
        """An execution engine over the current store, yahoo primary and finnhub fallback."""
        return ExecutionEngine(self.store, self.clock, self.calendar, self.yahoo, self.finnhub)

    def evaluation(
        self,
        reasoner: Reasoner,
        *,
        facts: list[Fact] | None = None,
        news_status: str = "ok",
        verifier: FakeSymbolVerifier | None = None,
        news_runner: NewsRunner | None = None,
    ) -> EvaluationEngine:
        """An evaluation engine. Its news stage returns ``facts`` unless a real ``news_runner`` is supplied."""

        def canned_news(cycle_id: str, _portfolio: PortfolioDoc) -> NewsResult:
            return NewsResult(
                cycle_id=cycle_id,
                news_status=news_status,
                facts=facts or [],
                uncovered_tickers=[],
                stats=NewsStats(),
            )

        return EvaluationEngine(
            self.store,
            self.clock,
            self.calendar,
            verifier or FakeSymbolVerifier(P_BASE),
            self.yahoo,
            self.finnhub,
            reasoner,
            news_runner or canned_news,
            make_reasoner_input_builder(self.store),
        )

    def prices(self, provider: str, **overrides: Script) -> None:
        """Override scripted prices on ``"yahoo"`` or ``"finnhub"`` (``prices("yahoo", MSFT="timeout")``)."""
        (self.yahoo if provider == "yahoo" else self.finnhub).script.update(overrides)

    # -- state fixtures (TS §2.4) -----------------------------------------------------------------------------

    def seed(self, status: str = "LIVE") -> PortfolioDoc:
        """S-LIVE (or another status): start_cash 50,000; AAPL 100 @ 200, JPM 50 @ 100, MSFT 20 @ 400; cash 17,000.

        MSFT is held so the standard pair fixtures need no new name; a buy of an unheld name is priced after the Reasoner
        names it (see ``test_new_symbol_buy_is_priced_and_proposed``).
        """
        trades = [
            make_trade(1, ticker="AAPL", shares="100", price="200"),
            make_trade(2, ticker="JPM", shares="50", price="100"),
            make_trade(3, ticker="MSFT", shares="20", price="400"),
        ]
        return self.seed_trades(trades, "50000", status)

    def seed_trades(self, trades: list[Trade], start_cash: str, status: str = "LIVE") -> PortfolioDoc:
        """Persist a self-consistent book (and its ``trades.log``) built from ``trades``."""
        doc = make_portfolio(trades, start_cash=start_cash, status=status)
        self.store.save_portfolio(doc)
        self.store.append_trades_log([format_log_line(t) for t in trades])
        return doc

    def put(self, doc: ProposalDoc) -> ProposalDoc:
        """Persist a proposal file."""
        self.store.save_proposal(doc.proposal_id, doc)
        return doc

    def proposal(self, proposal_id: str) -> ProposalDoc:
        """Read a proposal back from disk."""
        return ProposalDoc.model_validate_json(self.store.proposal_path(proposal_id).read_text(encoding="utf-8"))


def _leg(pid: str, leg: str, action: str, ticker: str, size: ProposalSize, shares: str, price: str, pair: str | None) -> ProposalLeg:
    value = Decimal(shares) * Decimal(price)
    return ProposalLeg(
        leg=leg,
        trade_id=f"{pid}-{leg}",
        pair=pair,
        action=action,  # type: ignore[arg-type]
        ticker=ticker,
        size=size,
        est_shares=Decimal(shares),
        est_price=Decimal(price),
        est_value=value,
        thesis="" if action == "SELL" else "thesis",
    )


def pr_pair(pid: str = PAIR_ID, state: str = "APPROVED", created_at: datetime = EVAL_SAT, approval_mode: str = "auto") -> ProposalDoc:
    """PR-PAIR: sell 25% of AAPL (25 sh, ~5,000), buy MSFT with 100% of the proceeds (12.5 sh @ 400)."""
    legs = [
        _leg(pid, "sell1", "SELL", "AAPL", ProposalSize(kind="fraction_of_position", fraction=Decimal("0.25")), "25", "200", "p1"),
        _leg(pid, "buy1", "BUY", "MSFT", ProposalSize(kind="proceeds_of", fraction=Decimal(1), source_leg="sell1"), "12.5", "400", "p1"),
    ]
    return _at_state(pid, legs, state, created_at, approval_mode)


def pr_buy(
    ticker: str, value: str, shares: str, price: str, pid: str = SINGLE_ID, state: str = "APPROVED"
) -> ProposalDoc:
    """A one-leg buy of ``value`` dollars of ``ticker`` (estimates ``shares`` @ ``price``)."""
    size = ProposalSize(kind="target_value", target_value=Decimal(value))
    return _at_state(pid, [_leg(pid, "buy1", "BUY", ticker, size, shares, price, None)], state, EVAL_SAT, "auto")


def pr_single(pid: str = SINGLE_ID, state: str = "APPROVED", created_at: datetime = EVAL_SAT, approval_mode: str = "auto") -> ProposalDoc:
    """PR-SINGLE: buy MSFT with 4,000 of cash (10 sh @ 400)."""
    legs = [_leg(pid, "buy1", "BUY", "MSFT", ProposalSize(kind="target_value", target_value=Decimal(4000)), "10", "400", None)]
    return _at_state(pid, legs, state, created_at, approval_mode)


def _at_state(pid: str, legs: list[ProposalLeg], state: str, created_at: datetime, approval_mode: str) -> ProposalDoc:
    """A proposal for the Monday open, walked along PROPOSED -> VETO_WINDOW -> APPROVED/... so its hash chain is legal."""
    doc = make_proposal(
        pid, state="PROPOSED", created_at=created_at, execute_at=SESSION_OPEN, approval_mode=approval_mode
    ).model_copy(update={"legs": legs})
    path = {
        "PROPOSED": [],
        "VETO_WINDOW": ["VETO_WINDOW"],
        "APPROVED": ["VETO_WINDOW", "APPROVED"],
        "VETOED": ["VETO_WINDOW", "VETOED"],
        "EXPIRED": ["VETO_WINDOW", "EXPIRED"],
    }[state]
    if "VETO_WINDOW" in path:
        doc = doc.model_copy(update={"veto_deadline": created_at + timedelta(hours=4)})
    for to in path:
        doc = advance(doc, to, created_at)  # type: ignore[arg-type]
    return doc
