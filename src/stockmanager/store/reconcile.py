"""Reconciliation: positions are a derived view of ``trades[]``, which must agree with ``trades.log`` (AS §1.6, §3.2)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from stockmanager.domain.docs import PortfolioDoc, Position, Trade
from stockmanager.domain.money import q4
from stockmanager.store import fsutil

LOG_PHRASE = "mock fill, no real money moved."
TOLERANCE = Decimal("0.0001")

# AS §3.2 parse regex. The id class admits lowercase because spec IDs carry a lowercase leg (``P-…-buy1``).
LOG_RE = re.compile(
    r"^(?P<id>[A-Za-z0-9-]+) \| (?P<ts>\S+) \| (?P<act>BUY|SELL) \| (?P<tkr>[A-Z][A-Z0-9.\-]{0,9}) \| "
    r"(?P<sh>\d+\.\d{4}) \| (?P<px>\d+\.\d{4}) \| (?P<cost>\d+\.\d{4}) \| (?P<src>yahoo|finnhub) \| "
    r"mock fill, no real money moved\.$"
)


class ReconcileError(Exception):
    """The log cannot be repaired by appending a suffix; a human must `RECONCILE rebuild` and ack."""


class InvalidHistory(Exception):
    """``trades[]`` cannot be replayed (negative cash, oversell, duplicate id)."""

    def __init__(self, trade_id: str, reason: str) -> None:
        super().__init__(f"{trade_id}: {reason}")
        self.trade_id = trade_id
        self.reason = reason


@dataclass(frozen=True)
class Replay:
    """Result of folding trades from nothing."""

    cash: Decimal | None
    positions: list[Position]


@dataclass
class LogComparison:
    """``trades[]`` versus ``trades.log``."""

    missing_suffix: list[str] = field(default_factory=list)  # trade_ids absent from the log, forming a suffix
    divergences: list[str] = field(default_factory=list)


def format_log_line(trade: Trade) -> str:
    """Render one ``trades.log`` line (AS §3.2)."""
    return (
        f"{trade.trade_id} | {trade.timestamp.isoformat()} | {trade.action} | {trade.ticker} | "
        f"{trade.shares} | {trade.fill_price} | {trade.cost} | {trade.price_source} | {LOG_PHRASE}"
    )


def replay(start_cash: Decimal | None, trades: list[Trade]) -> Replay:
    """Fold trades in order using average cost. ``start_cash=None`` skips the cash checks and returns ``cash=None``."""
    cash = start_cash
    held: dict[str, Position] = {}
    seen: set[str] = set()
    for t in trades:
        if t.trade_id in seen:
            raise InvalidHistory(t.trade_id, "duplicate trade_id")
        seen.add(t.trade_id)
        cur = held.get(t.ticker)
        if t.action == "BUY":
            if cash is not None:
                cash = q4(cash - t.cost)
                if cash < 0:
                    raise InvalidHistory(t.trade_id, f"negative cash {cash}")
            shares = q4((cur.shares if cur else Decimal(0)) + t.shares)
            basis = q4((cur.cost_basis if cur else Decimal(0)) + t.cost)
            held[t.ticker] = Position(
                ticker=t.ticker,
                shares=shares,
                fill_price=q4(basis / shares),
                fill_time=cur.fill_time if cur else t.timestamp,
                cost_basis=basis,
                thesis=t.thesis or (cur.thesis if cur else ""),
                price_source=t.price_source,
            )
        else:
            if cur is None or t.shares > cur.shares:
                raise InvalidHistory(t.trade_id, f"sell {t.shares} {t.ticker} exceeds held {cur.shares if cur else 0}")
            if cash is not None:
                cash = q4(cash + t.cost)
            remaining = q4(cur.shares - t.shares)
            if remaining == 0:
                del held[t.ticker]
                continue
            basis = q4(cur.cost_basis * remaining / cur.shares)
            held[t.ticker] = cur.model_copy(
                update={
                    "shares": remaining,
                    "cost_basis": basis,
                    "fill_price": q4(basis / remaining),
                    "price_source": t.price_source,
                }
            )
    return Replay(cash=cash, positions=list(held.values()))


def rebuild_positions(trades: list[Trade]) -> list[Position]:
    """Derive ``positions[]`` from the append-only trades (AS §3.1). Raises ``InvalidHistory``."""
    return replay(None, trades).positions


def reconcile_portfolio(portfolio: PortfolioDoc) -> tuple[PortfolioDoc, list[str]]:
    """Replay ``trades[]`` and compare with the stored ``cash`` and ``positions[]``.

    Returns the portfolio with ``cash`` and ``positions[]`` as derived, and one message per divergence between the
    stored and derived values (``|Δ| > 0.0001``; extra or missing tickers count). The input is not modified, and
    ``trades[]`` is never touched. Raises ``InvalidHistory`` if the trades cannot be replayed.
    """
    derived = replay(portfolio.start_cash, portfolio.trades)
    assert derived.cash is not None
    out: list[str] = []
    if abs(portfolio.cash - derived.cash) > TOLERANCE:
        out.append(f"cash: stored {portfolio.cash}, derived {derived.cash}")
    stored = {p.ticker: p for p in portfolio.positions}
    want = {p.ticker: p for p in derived.positions}
    for ticker in sorted(stored.keys() | want.keys()):
        s, d = stored.get(ticker), want.get(ticker)
        if s is None:
            out.append(f"{ticker}: missing from positions[], derived {d.shares if d else 0} shares")
        elif d is None:
            out.append(f"{ticker}: in positions[] ({s.shares} shares) but trades[] hold none")
        else:
            if abs(s.shares - d.shares) > TOLERANCE:
                out.append(f"{ticker}: shares stored {s.shares}, derived {d.shares}")
            if abs(s.cost_basis - d.cost_basis) > TOLERANCE:
                out.append(f"{ticker}: cost_basis stored {s.cost_basis}, derived {d.cost_basis}")
    # Keep user-edited thesis and the stored record's source where the position exists on both sides.
    positions = [
        d.model_copy(update={"thesis": stored[d.ticker].thesis or d.thesis}) if d.ticker in stored else d
        for d in derived.positions
    ]
    return portfolio.model_copy(update={"cash": derived.cash, "positions": positions}), out


def read_log_lines(log_path: Path) -> list[str]:
    """Lines of ``trades.log`` (empty if absent). A torn final line is returned as-is, without a newline."""
    if not log_path.exists():
        return []
    parts = log_path.read_text(encoding="utf-8").split("\n")
    return parts[:-1] if parts[-1] == "" else parts


def compare_log(trades: list[Trade], log_lines: list[str]) -> LogComparison:
    """AS §1.6 step 3: ids absent from the log that form a suffix of ``trades[]`` are repairable; all else diverges."""
    result = LogComparison()
    by_id = {t.trade_id: t for t in trades}
    present: set[str] = set()
    for n, line in enumerate(log_lines, start=1):
        m = LOG_RE.match(line)
        if m is None:
            result.divergences.append(f"line {n}: unparsable or torn")
            continue
        tid = m["id"]
        trade = by_id.get(tid)
        if trade is None:
            result.divergences.append(f"line {n}: {tid} has no trade")
        elif tid in present:
            result.divergences.append(f"line {n}: duplicate {tid}")
        elif (diff := _mismatch(trade, m)) is not None:
            result.divergences.append(f"line {n}: {tid} {diff}")
        present.add(tid)
    missing = [t.trade_id for t in trades if t.trade_id not in present]
    first = len(trades) - len(missing)
    if all(t.trade_id in present for t in trades[:first]):
        result.missing_suffix = missing
    else:
        result.divergences.extend(f"{tid}: missing from trades.log (not a suffix)" for tid in missing)
    return result


def _mismatch(trade: Trade, m: re.Match[str]) -> str | None:
    """First field that differs between a trade and its log line, as text."""
    try:
        ts = datetime.fromisoformat(m["ts"])
    except ValueError:
        return f"bad timestamp {m['ts']!r}"
    checks: list[tuple[str, bool]] = [
        ("timestamp", ts == trade.timestamp),
        ("action", m["act"] == trade.action),
        ("ticker", m["tkr"] == trade.ticker),
        ("shares", Decimal(m["sh"]) == trade.shares),
        ("price", Decimal(m["px"]) == trade.fill_price),
        ("cost", Decimal(m["cost"]) == trade.cost),
        ("source", m["src"] == trade.price_source),
    ]
    for name, ok in checks:
        if not ok:
            return f"{name} differs from trades[]"
    return None


def repair_log_suffix(log_path: Path, expected_lines: list[str]) -> list[str]:
    """Append the lines of ``expected_lines`` (the full expected log) that the log lacks; returns what was appended.

    The existing log must be an exact prefix of ``expected_lines``; otherwise (torn line, extra line, mismatch) this
    raises ``ReconcileError`` and writes nothing. It never truncates or rewrites.
    """
    have = read_log_lines(log_path)
    if have != expected_lines[: len(have)]:
        raise ReconcileError("trades.log is not a prefix of the expected log; suffix repair refused")
    missing = expected_lines[len(have) :]
    if missing:
        fsutil.append_lines(log_path, missing)
    return missing
