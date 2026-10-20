"""Execution engine (AS §1.3, §8.6): turns an approved proposal, the opening, or a close confirm into fills.

No judgement is applied here -- legs, caps and timing were decided at proposal time; this engine only re-validates
them against the current books and actual fill prices, then applies them. ``domain.fills`` carries every bit of
position/cash arithmetic (``size_buy``, ``size_sell``, ``clamp_to_cash``, ``apply_fill``), so it is unit-testable
in isolation and shared with reconcile's replay.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from stockmanager.domain.docs import PortfolioDoc, PriceQuote, ProposalDoc, Trade, TransitionRecord
from stockmanager.domain.fills import LedgerState, apply_fill, clamp_to_cash, size_buy, size_sell
from stockmanager.domain.ids import opening_proposal_id, trade_id
from stockmanager.domain.money import q4
from stockmanager.domain.ports import PriceProvider, SessionCalendar
from stockmanager.domain.proposal_state import GENESIS_HASH, ProposalState, can_transition, chain_hash
from stockmanager.notify.outbox import prepare
from stockmanager.notify.templates import price_source_failure, trade_executed
from stockmanager.pricing.errors import PriceError, ValuationUnavailable
from stockmanager.pricing.service import PriceService
from stockmanager.store import FileStore
from stockmanager.store.lock import Clock, LockBusy
from stockmanager.store.reconcile import format_log_line
from stockmanager.store.transaction import GateError

Outcome = Literal[
    "completed",
    "skipped_paused",
    "skipped_not_live",
    "halted_reconcile",
    "failed_closed",
    "lock_busy",
    "aborted_state_changed",
    "voided",
    "no_op",
]


@dataclass(frozen=True)
class RunResult:
    """What one execution run did (AS §1.3)."""

    outcome: Outcome
    detail: str = ""


def _transition(doc: ProposalDoc, to_state: ProposalState, actor: str, cause: str, at: datetime) -> ProposalDoc:
    prev = doc.transitions[-1]
    rec = TransitionRecord(
        seq=prev.seq + 1, from_state=doc.state, to=to_state, at=at, actor=actor, cause=cause, prev_hash=prev.hash, hash=""
    )
    rec = rec.model_copy(update={"hash": chain_hash(prev.hash, rec.model_dump(mode="json", by_alias=True))})
    return doc.model_copy(update={"state": to_state, "transitions": [*doc.transitions, rec]})


def _chain_valid(doc: ProposalDoc) -> bool:
    """Re-validate the whole ``transitions[]`` hash chain and its legality (AS §3.3, TS NN-07)."""
    prev_hash = GENESIS_HASH
    prev_to: ProposalState | None = None
    for i, t in enumerate(doc.transitions, start=1):
        if t.seq != i or t.prev_hash != prev_hash or t.from_state != prev_to:
            return False
        if i > 1 and not can_transition(prev_to, t.to):  # type: ignore[arg-type]
            return False
        if t.hash != chain_hash(prev_hash, t.model_dump(mode="json", by_alias=True)):
            return False
        prev_hash, prev_to = t.hash, t.to
    return doc.state == doc.transitions[-1].to


def _load_proposal(store: FileStore, proposal_id: str) -> ProposalDoc | None:
    path = store.proposal_path(proposal_id)
    if not path.exists():
        return None
    try:
        return ProposalDoc.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _load_all_proposals(store: FileStore) -> list[ProposalDoc]:
    directory = store.root / "proposals"
    if not directory.exists():
        return []
    out: list[ProposalDoc] = []
    for path in sorted(directory.glob("*.json")):
        try:
            out.append(ProposalDoc.model_validate_json(path.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return out


def _group_legs(doc: ProposalDoc) -> list[list[str]]:
    """Each pair of legs that fund each other is one group; every independent leg is its own group (AS §8.6 step 2)."""
    paired: dict[str, set[str]] = {}
    singles: list[list[str]] = []
    for leg in doc.legs:
        if leg.pair:
            paired.setdefault(leg.pair, set()).add(leg.ticker)
        else:
            singles.append([leg.ticker])
    return [sorted(tickers) for tickers in paired.values()] + singles


class ExecutionEngine:
    """``run_opening`` / ``run_proposal`` / ``run_final_valuation`` (AS §1.3, §8.6)."""

    def __init__(
        self,
        store: FileStore,
        clock: Clock,
        calendar: SessionCalendar,
        price_primary: PriceProvider,
        price_fallback: PriceProvider,
        *,
        exec_delay_minutes: int = 5,
    ) -> None:
        self._store = store
        self._clock = clock
        self._calendar = calendar
        self._primary = price_primary
        self._fallback = price_fallback
        self._prices = PriceService()
        self._exec_delay = exec_delay_minutes

    # -- run_proposal: AS §8.6 steps 1-11 -----------------------------------------------------------------------

    def run_proposal(self, proposal_id: str) -> RunResult:
        """Execute an ``APPROVED`` proposal (AS §8.6). Fails closed; never partially fills."""
        # Step 1: pre-flight, no lock.
        doc = _load_proposal(self._store, proposal_id)
        if doc is None:
            return RunResult("no_op", f"no such proposal {proposal_id}")
        if not self._store.portfolio_exists():
            return RunResult("skipped_not_live", "no portfolio")
        portfolio = self._store.load_portfolio()
        if portfolio.status != "LIVE":
            return RunResult("skipped_not_live", portfolio.status)
        if doc.state != "APPROVED":
            return RunResult("no_op", f"{proposal_id} is {doc.state}")
        now = self._clock()
        window_start = doc.execute_at + timedelta(minutes=self._exec_delay)
        if not (window_start <= now <= doc.expires_at):
            return RunResult("no_op", "outside the execution window; the sweeper handles expiry")

        # Step 2: group legs.
        groups = _group_legs(doc)

        # Step 3: prices, no lock.
        try:
            quotes = self._prices.fill_prices(groups, doc.fill_session_date, self._primary, self._fallback)
        except (PriceError, ValuationUnavailable) as exc:
            self._alert_price_failure(proposal_id, doc, str(exc), now)
            return RunResult("failed_closed", str(exc))

        # Step 4: transaction (lock, re-read, gate 2, reconcile).
        try:
            with self._store.transaction("execution:proposal") as txn:
                txn.require(status="LIVE")
                txn.reconcile()
                cur = _load_proposal(self._store, proposal_id)
                if cur is None:
                    return RunResult("aborted_state_changed", "proposal file vanished")

                # Step 5: re-validate.
                if not _chain_valid(cur):
                    return RunResult("aborted_state_changed", "transitions[] chain is invalid")
                if now > cur.expires_at:
                    txn.stage_proposals({proposal_id: _transition(cur, "EXPIRED", "system:sweeper", "past expires_at", now)})
                    return RunResult("completed", "expired at execution time")
                existing_ids = {t.trade_id for t in txn.state.trades}
                if cur.legs and all(leg.trade_id in existing_ids for leg in cur.legs):
                    txn.stage_proposals({proposal_id: _transition(cur, "EXECUTED", "system:execution", "already filled", now)})
                    return RunResult("completed", "already executed (CP4 recovery)")
                if cur.state != "APPROVED":
                    return RunResult("aborted_state_changed", f"now {cur.state} (a veto or pause won the race)")

                # Step 6: plan (pure) -- sells first, buys after.
                held = {p.ticker: p.shares for p in txn.state.positions}
                cash = txn.state.cash
                proceeds: dict[str, Decimal] = {}
                trades: list[Trade] = []
                for leg in sorted(cur.legs, key=lambda x: x.action != "SELL"):
                    if leg.trade_id in existing_ids:  # Step 8: idempotency guard (done per-leg, inline).
                        continue
                    quote = quotes.get(leg.ticker)
                    if quote is None:
                        return RunResult("failed_closed", f"no quote for {leg.ticker}")
                    if leg.action == "SELL":
                        shares = size_sell(held.get(leg.ticker, Decimal(0)), leg.size.fraction or Decimal(0))
                        if shares <= 0:
                            continue
                        value = q4(shares * quote.price)
                        proceeds[leg.leg] = value
                        held[leg.ticker] = q4(held.get(leg.ticker, Decimal(0)) - shares)
                        cash = q4(cash + value)
                        trades.append(self._trade(leg.trade_id, now, "SELL", leg.ticker, shares, quote, ""))
                    else:
                        budget = (
                            proceeds.get(leg.size.source_leg or "", Decimal(0)) * (leg.size.fraction or Decimal(0))
                            if leg.size.kind == "proceeds_of"
                            else leg.size.target_value or Decimal(0)
                        )
                        shares = clamp_to_cash(size_buy(budget, quote.price), quote.price, cash)
                        if shares <= 0:
                            continue
                        cost = q4(shares * quote.price)
                        cash = q4(cash - cost)
                        held[leg.ticker] = q4(held.get(leg.ticker, Decimal(0)) + shares)
                        trades.append(self._trade(leg.trade_id, now, "BUY", leg.ticker, shares, quote, leg.thesis))

                # Step 7: void checks (whole-proposal; no partial or resized fill survives a void).
                void_reason = self._void_reason(cur, txn.state, quotes, held, cash, len(trades))
                if void_reason is not None:
                    txn.stage_proposals({proposal_id: _transition(cur, "VOIDED", "system:execution", void_reason, now)})
                    return RunResult("voided", void_reason)

                # Step 9: apply.
                ledger = LedgerState(cash=txn.state.cash, positions={p.ticker: p for p in txn.state.positions})
                for t in trades:
                    ledger = apply_fill(ledger, t)

                # Step 10: commit.
                new_portfolio = txn.state.model_copy(
                    update={
                        "cash": ledger.cash,
                        "positions": list(ledger.positions.values()),
                        "trades": [*txn.state.trades, *trades],
                    }
                )
                txn.stage_portfolio(new_portfolio)
                if trades:
                    txn.stage_log([format_log_line(t) for t in trades])
                txn.stage_proposals({proposal_id: _transition(cur, "EXECUTED", "system:execution", "filled", now)})
                alerts = {}
                for t in trades:
                    a = prepare(trade_executed(t.trade_id, t.action, t.ticker, t.shares, t.cost), now)
                    alerts[f"{a.alert_id}.json"] = a.to_json()
                txn.stage_alerts(alerts)
        except LockBusy:
            return RunResult("lock_busy")
        except GateError as exc:
            return RunResult("skipped_not_live", str(exc))
        # Step 11: unlock; alert delivery is best-effort and happens on the next tick's outbox drain.
        return RunResult("completed")

    def _void_reason(
        self,
        doc: ProposalDoc,
        portfolio: PortfolioDoc,
        quotes: dict[str, PriceQuote],
        held_after: dict[str, Decimal],
        cash_after: Decimal,
        trade_count: int,
    ) -> str | None:
        """AS §8.6 step 7: cash, re-checked position cap at actual fill prices, trade count."""
        if cash_after < 0:
            return "insufficient_cash"
        if trade_count > doc.caps.max_trades_per_cycle:
            return "cap_breach_at_execution"
        # Untraded holdings are marked at their last valuation price (fill_price) -- ⚑AD15.
        last_price = {p.ticker: p.fill_price for p in portfolio.positions}
        prices = {**last_price, **{t: q.price for t, q in quotes.items()}}
        nav = q4(cash_after + sum((held_after[t] * prices.get(t, Decimal(0)) for t in held_after), Decimal(0)))
        if nav <= 0:
            return None
        for leg in doc.legs:
            if leg.action != "BUY":
                continue
            weight = held_after.get(leg.ticker, Decimal(0)) * prices.get(leg.ticker, Decimal(0)) / nav * 100
            if weight > doc.caps.max_position_pct:
                return "cap_breach_at_execution"
        return None

    def _alert_price_failure(self, proposal_id: str, doc: ProposalDoc, detail: str, now: datetime) -> None:
        tickers = sorted({leg.ticker for leg in doc.legs})
        alert = prepare(price_source_failure(f"execution:{proposal_id}", tickers, fallback_worked=False), now)
        alert.body = f"{alert.body} ({detail})"
        try:
            self._store.save_extras(alerts={f"{alert.alert_id}.json": alert.to_json()})
        except OSError:
            pass  # best-effort; the next tick retries the fill and can re-raise the alert

    @staticmethod
    def _trade(
        trade_id_: str, at: datetime, action: Literal["BUY", "SELL"], ticker: str, shares: Decimal, quote: PriceQuote, thesis: str
    ) -> Trade:
        return Trade(
            trade_id=trade_id_,
            timestamp=at,
            action=action,
            ticker=ticker,
            shares=shares,
            fill_price=quote.price,
            cost=q4(shares * quote.price),
            price_source=quote.source,
            thesis=thesis,
        )

    # -- run_opening: all-or-nothing (TS NN-15) ------------------------------------------------------------------

    def run_opening(self) -> RunResult:
        """``APPROVED`` -> ``LIVE``: fill every staged ticker's open. All-or-nothing (TS NN-15, ⚑AD7)."""
        if not self._store.portfolio_exists():
            return RunResult("skipped_not_live", "no portfolio")
        snapshot = self._store.load_portfolio()
        if snapshot.status != "APPROVED":
            return RunResult("skipped_not_live", snapshot.status)
        now = self._clock()
        session_open = self._calendar.next_session_open_at_or_after(now)
        if session_open > now:
            return RunResult("no_op", "the session has not opened yet; re-arms next tick")
        staged: list[dict[str, Any]] = list(snapshot.config.get("staged_orders", []))
        if not staged:
            return RunResult("no_op", "nothing staged")
        session_date = session_open.date()
        groups = [[row["ticker"]] for row in staged]  # independent single-leg fills (TS EDGE-30)
        try:
            quotes = self._prices.fill_prices(groups, session_date, self._primary, self._fallback)
        except (PriceError, ValuationUnavailable) as exc:
            self._alert_opening_failure(staged, str(exc), now)
            return RunResult("failed_closed", str(exc))
        if any(row["ticker"] not in quotes for row in staged):  # belt-and-braces: all-or-nothing
            return RunResult("failed_closed", "not every staged ticker was priced")

        try:
            with self._store.transaction("execution:opening") as txn:
                txn.require(status="APPROVED")
                txn.reconcile()
                pid = opening_proposal_id(session_date)
                cash = txn.state.cash
                trades: list[Trade] = []
                for n, row in enumerate(staged, start=1):
                    ticker = row["ticker"]
                    quote = quotes[ticker]
                    value = Decimal(str(row["target_value"]))
                    shares = size_buy(value, quote.price)
                    shares = clamp_to_cash(shares, quote.price, cash)
                    if shares <= 0:
                        continue
                    cost = q4(shares * quote.price)
                    cash = q4(cash - cost)
                    trades.append(self._trade(trade_id(pid, f"buy{n}"), session_open, "BUY", ticker, shares, quote, str(row.get("thesis", ""))))
                if not trades:
                    return RunResult("no_op", "no fillable tickers")
                ledger = LedgerState(cash=txn.state.cash, positions={p.ticker: p for p in txn.state.positions})
                for t in trades:
                    ledger = apply_fill(ledger, t)
                new_doc = txn.state.model_copy(
                    update={
                        "status": "LIVE",
                        "cash": ledger.cash,
                        "positions": list(ledger.positions.values()),
                        "trades": [*txn.state.trades, *trades],
                    }
                )
                txn.stage_portfolio(new_doc)
                txn.stage_log([format_log_line(t) for t in trades])
                txn.stage_extras(
                    nav_rows=[
                        {
                            "schema_version": 1,
                            "date": session_date.isoformat(),
                            "kind": "inception",
                            "nav": str(new_doc.cash + sum((p.shares * p.fill_price for p in new_doc.positions), Decimal(0))),
                            "cash": str(new_doc.cash),
                            "recorded_at": self._clock().isoformat(),
                        }
                    ]
                )
                now2 = self._clock()
                alerts = {}
                for t in trades:
                    a = prepare(trade_executed(t.trade_id, t.action, t.ticker, t.shares, t.cost), now2)
                    alerts[f"{a.alert_id}.json"] = a.to_json()
                txn.stage_alerts(alerts)
        except LockBusy:
            return RunResult("lock_busy")
        except GateError as exc:
            return RunResult("skipped_not_live", str(exc))
        return RunResult("completed")

    def _alert_opening_failure(self, staged: Sequence[dict[str, Any]], detail: str, now: datetime) -> None:
        tickers = sorted({row["ticker"] for row in staged})
        alert = prepare(price_source_failure("execution:opening", tickers, fallback_worked=False), now)
        alert.body = f"{alert.body} ({detail}) -- status stays APPROVED; re-arms next session."
        try:
            self._store.save_extras(alerts={f"{alert.alert_id}.json": alert.to_json()})
        except OSError:
            pass

    # -- run_final_valuation: CLOSE CONFIRM ----------------------------------------------------------------------

    def run_final_valuation(self, benchmark: str | None = None) -> RunResult:
        """``CLOSE CONFIRM`` (AS §8.6): fetch closes before the lock; one transaction records the final report.

        Positions are **not** sold -- ``trades[]`` is unchanged (TS CMD-C02). All open proposals move to
        ``CANCELLED_CLOSED`` and ``state/pending_close.json`` is removed.
        """
        if not self._store.portfolio_exists():
            return RunResult("skipped_not_live", "no portfolio")
        snapshot = self._store.load_portfolio()
        if snapshot.status not in ("LIVE", "PAUSED"):
            return RunResult("skipped_not_live", snapshot.status)
        now = self._clock()
        session_date = now.date()
        tickers = sorted({p.ticker for p in snapshot.positions} | ({benchmark} if benchmark else set()))
        prices: dict[str, Decimal] = {}
        source = "yahoo"
        if tickers:
            try:
                valuation = self._prices.valuation(tickers, session_date, self._primary, self._fallback)
            except ValuationUnavailable as exc:
                return RunResult("failed_closed", str(exc))
            prices = {t: q.price for t, q in valuation.quotes.items()}
            source = valuation.source

        try:
            with self._store.transaction("execution:final_valuation") as txn:
                txn.require(status=("LIVE", "PAUSED"))
                txn.reconcile()
                positions_value = q4(sum((p.shares * prices[p.ticker] for p in txn.state.positions), Decimal(0)))
                nav = q4(txn.state.cash + positions_value)
                final_report = {
                    "ending_value": str(nav),
                    "roi_pct": str((nav - txn.state.start_cash) / txn.state.start_cash * 100) if txn.state.start_cash > 0 else None,
                    "source": source,
                    "as_of": session_date.isoformat(),
                }
                new_doc = txn.state.model_copy(update={"status": "CLOSED", "closed_at": now, "final_report": final_report})
                txn.stage_portfolio(new_doc)
                txn.stage_extras(
                    nav_rows=[
                        {
                            "schema_version": 1,
                            "date": session_date.isoformat(),
                            "kind": "final",
                            "nav": str(nav),
                            "cash": str(txn.state.cash),
                            "positions_value": str(positions_value),
                            "source": source,
                            "recorded_at": now.isoformat(),
                        }
                    ]
                )
                staged: dict[str, ProposalDoc] = {}
                for p in _load_all_proposals(self._store):
                    if can_transition(p.state, "CANCELLED_CLOSED"):
                        staged[p.proposal_id] = _transition(p, "CANCELLED_CLOSED", "system:close", "system closed", now)
                if staged:
                    txn.stage_proposals(staged)
        except LockBusy:
            return RunResult("lock_busy")
        except GateError as exc:
            return RunResult("skipped_not_live", str(exc))
        pending_close = self._store.root / "state" / "pending_close.json"
        pending_close.unlink(missing_ok=True)
        return RunResult("completed")
