"""ProposalBuilder (AS §8.2, phase C): turns the Reasoner's *intent* into validated ``ProposalDoc`` objects.

All numbers come from code. The model's sizing intent (a fraction, a source leg, a dollar target) is converted to
estimated shares at the valuation prices; no model-supplied number is ever read as a share count or a price. Ideas
that cannot be made valid are dropped whole into ``not_proposed``; buys that breach a cap are resized or dropped
*before* the veto window opens.

Buy shares are floored (a buy never spends more than its budget); sell shares are ``held x fraction`` rounded
half-up, as at execution (AS §3.3).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from stockmanager.config.proposals import ProposalConfig
from stockmanager.domain.docs import (
    CapsSnapshot,
    NotProposed,
    NotProposedReason,
    PortfolioDoc,
    PriceTable,
    ProposalDoc,
    ProposalLeg,
    ProposalSize,
    TransitionRecord,
)
from stockmanager.domain.ids import proposal_id, trade_id
from stockmanager.domain.money import Q4, q4
from stockmanager.domain.ports import SessionCalendar, SymbolVerifier
from stockmanager.domain.proposal_state import GENESIS_HASH, chain_hash
from stockmanager.domain.timeutil import add_elapsed
from stockmanager.evaluation.reasoner_io import ReasonerOutput, TradeIdea
from stockmanager.news.config import TICKER_RE
from stockmanager.store.lock import Clock

log = logging.getLogger(__name__)

PT = ZoneInfo("America/Los_Angeles")
_LABEL = re.compile(r"(sell|buy)[1-9]")
_LINE_BREAKS = str.maketrans({c: " " for c in "\n\r\t\v\f\x85  "})
THESIS_MAX_CHARS = 300
RATIONALE_MAX_CHARS = 600
HOLD_ALTERNATIVE_MAX_CHARS = 1200
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class BuildResult:
    """Proposals to persist and the ideas that did not become proposals, with reasons."""

    proposals: list[ProposalDoc]
    not_proposed: list[NotProposed]
    proposal_ideas: dict[str, int] = field(default_factory=dict)  # proposal id -> priority of the idea it came from


class _Drop(Exception):
    """An idea cannot be proposed."""

    def __init__(self, reason: NotProposedReason, detail: str = "") -> None:
        super().__init__(reason, detail)
        self.reason: NotProposedReason = reason
        self.detail = detail


Action = Literal["BUY", "SELL"]
Kind = Literal["fraction_of_position", "proceeds_of", "target_value"]


@dataclass(frozen=True)
class _Leg:
    """A leg under construction. ``fraction`` and ``value`` are the sizing parameters; shares and value derive."""

    label: str
    action: Action
    ticker: str
    thesis: str
    kind: Kind
    fraction: Decimal | None
    source: str | None
    value: Decimal | None
    price: Decimal
    shares: Decimal = Decimal(0)
    est_value: Decimal = Decimal(0)


def _sanitise(text: str, limit: int) -> str:
    """NFC, drop Cc/Cf, collapse to one line, cut to ``limit`` (AS §7.5 rules for stored model prose)."""
    nfc = unicodedata.normalize("NFC", text).translate(_LINE_BREAKS)
    kept = "".join(c for c in nfc if unicodedata.category(c) not in ("Cc", "Cf"))
    return " ".join(kept.split())[:limit].rstrip()


def _down(value: Decimal) -> Decimal:
    return value.quantize(Q4, rounding=ROUND_DOWN)


class ProposalBuilder:
    """Builds proposals from intent. Stateless apart from the calendar and symbol-verifier seams."""

    def __init__(self, calendar: SessionCalendar, verifier: SymbolVerifier) -> None:
        self._calendar = calendar
        self._verifier = verifier

    def unheld_buy_tickers(self, intent: ReasonerOutput, snapshot: PortfolioDoc) -> list[str]:
        """Tickers the intent buys that the book does not hold and that may be priced (AS §8.2 step 1).

        Only a well-formed, verified US equity or ETF qualifies, so nothing the model made up is ever sent to a price
        provider. ``build`` applies the same test again when it validates the idea.
        """
        held = {p.ticker for p in snapshot.positions}
        names = {
            leg.ticker
            for idea in intent.ideas
            for leg in idea.legs
            if leg.action == "BUY" and leg.ticker not in held
        }
        return sorted(t for t in names if TICKER_RE.fullmatch(t) and self._verifier.is_valid_us_equity_or_etf(t))

    def build(
        self,
        intent: ReasonerOutput,
        snapshot: PortfolioDoc,
        valuation: PriceTable,
        cfg: ProposalConfig,
        clock: Clock,
        existing_ids: Collection[str] = (),
    ) -> BuildResult:
        """Apply AS §8.2 steps 1-8 to every idea. ``existing_ids`` are the proposal IDs already on disk.

        Raises ``ValueError`` if ``valuation`` does not price every holding (it must be a complete single-source table).
        """
        created_at = clock().astimezone(PT)
        held = {p.ticker: p.shares for p in snapshot.positions}
        prices = {t: q.price for t, q in valuation.quotes.items()}
        missing = sorted(set(held) - set(prices))
        if missing:
            raise ValueError(f"valuation does not price holdings: {missing}")
        nav = q4(snapshot.cash + sum((held[t] * prices[t] for t in held), Decimal(0)))
        if nav <= 0:
            raise ValueError("NAV is not positive")

        not_proposed: list[NotProposed] = []
        valid: list[tuple[TradeIdea, list[_Leg]]] = []
        for idea in sorted(intent.ideas, key=lambda i: i.priority):  # stable: ties keep list order
            try:
                valid.append((idea, self._estimate(self._validate(idea, held, prices), held)))
            except _Drop as d:
                not_proposed.append(NotProposed(priority=idea.priority, reason=d.reason, detail=d.detail))

        # Step 4: admit whole ideas in priority order; the first that would exceed the cap and all after it are cut.
        admitted: list[tuple[TradeIdea, list[_Leg]]] = []
        legs_used = 0
        for position, (idea, legs) in enumerate(valid):
            if legs_used + len(legs) > cfg.max_trades_per_cycle:
                not_proposed += [
                    NotProposed(priority=i.priority, reason="trade_cap", detail=f"{len(ls)} legs")
                    for i, ls in valid[position:]
                ]
                break
            legs_used += len(legs)
            admitted.append((idea, legs))

        # Steps 5-6 and the cross-idea checks run over running totals; only an accepted idea updates them.
        bought: dict[str, Decimal] = {}
        sold: dict[str, Decimal] = {}
        cash_used = Decimal(0)
        accepted: list[tuple[TradeIdea, list[_Leg]]] = []
        for idea, legs in admitted:
            try:
                legs = self._fit_caps(legs, held, prices, nav, snapshot.cash - cash_used, bought, cfg)
                self._check_oversell(legs, held, sold)
            except _Drop as d:
                not_proposed.append(NotProposed(priority=idea.priority, reason=d.reason, detail=d.detail))
                continue
            for leg in legs:
                if leg.action == "BUY":
                    bought[leg.ticker] = bought.get(leg.ticker, Decimal(0)) + leg.est_value
                    if leg.kind == "target_value":
                        cash_used += leg.est_value
                else:
                    sold[leg.ticker] = sold.get(leg.ticker, Decimal(0)) + leg.shares
            accepted.append((idea, legs))

        proposals = self._assign(accepted, intent, cfg, created_at, existing_ids)
        links = {p.proposal_id: idea.priority for p, (idea, _) in zip(proposals, accepted, strict=True)}
        return BuildResult(proposals, not_proposed, links)

    # -- step 1 and 2 ----------------------------------------------------------------------------------------------

    def _validate(self, idea: TradeIdea, held: dict[str, Decimal], prices: dict[str, Decimal]) -> list[_Leg]:
        if not idea.legs:
            raise _Drop("malformed_idea", "no legs")
        labels = [leg.leg for leg in idea.legs]
        if len(set(labels)) != len(labels):
            raise _Drop("malformed_idea", "duplicate leg labels")
        by_label = {leg.leg: leg for leg in idea.legs}
        sells = {leg.ticker for leg in idea.legs if leg.action == "SELL"}
        out: list[_Leg] = []
        for leg in idea.legs:
            m = _LABEL.fullmatch(leg.leg)
            if m is None or m.group(1).upper() != leg.action:
                raise _Drop("malformed_idea", f"bad leg label {leg.leg!r} for {leg.action}")
            size = leg.size_intent
            fraction = Decimal(size.fraction) if size.fraction is not None else None
            value = Decimal(size.value) if size.value is not None else None
            if leg.action == "SELL":
                if size.kind != "fraction_of_position":
                    raise _Drop("malformed_idea", f"{leg.leg}: a sell needs fraction_of_position")
                if leg.ticker not in held:
                    raise _Drop("short_or_oversell", f"{leg.ticker} is not held")
                if fraction is None or not Decimal(0) < fraction <= Decimal(1):
                    raise _Drop("short_or_oversell", f"{leg.leg}: fraction outside (0, 1]")
                thesis = ""
            else:
                if size.kind == "fraction_of_position":
                    raise _Drop("malformed_idea", f"{leg.leg}: a buy cannot be fraction_of_position")
                if leg.ticker in sells:
                    raise _Drop("malformed_idea", f"{leg.ticker} is both bought and sold")
                if leg.ticker not in held and not (
                    TICKER_RE.fullmatch(leg.ticker) and self._verifier.is_valid_us_equity_or_etf(leg.ticker)
                ):
                    raise _Drop("invalid_symbol", leg.ticker)
                if size.kind == "proceeds_of":
                    source = by_label.get(size.leg or "")
                    if source is None or source.action != "SELL":
                        raise _Drop("pairing", f"{leg.leg}: proceeds_of {size.leg!r} is not a SELL leg in this idea")
                    if fraction is None or not Decimal(0) < fraction <= Decimal(1):
                        raise _Drop("malformed_idea", f"{leg.leg}: proceeds fraction outside (0, 1]")
                elif value is None or value <= 0:
                    raise _Drop("malformed_idea", f"{leg.leg}: target_value must be positive")
                thesis = _sanitise(leg.thesis or "", THESIS_MAX_CHARS)
                if not thesis:
                    raise _Drop("malformed_idea", f"{leg.leg}: a buy needs a thesis")
            if leg.ticker not in prices:
                raise _Drop("unpriced", leg.ticker)
            out.append(
                _Leg(leg.leg, leg.action, leg.ticker, thesis, size.kind, fraction, size.leg, value, prices[leg.ticker])
            )
        return sorted(out, key=lambda leg: leg.action != "SELL")  # sells first, stable

    # -- step 3 ----------------------------------------------------------------------------------------------------

    @staticmethod
    def _estimate(legs: list[_Leg], held: dict[str, Decimal]) -> list[_Leg]:
        """Shares and value for every leg at valuation prices, from the sizing parameters only."""
        done: dict[str, _Leg] = {}
        for leg in legs:  # sells come first, so a proceeds_of source is always done before its buy
            if leg.action == "SELL":
                assert leg.fraction is not None
                shares = q4(min(held[leg.ticker] * leg.fraction, held[leg.ticker]))
                budget = Decimal(0)
            else:
                if leg.kind == "proceeds_of":
                    assert leg.source is not None and leg.fraction is not None
                    budget = done[leg.source].est_value * leg.fraction
                else:
                    assert leg.value is not None
                    budget = leg.value
                shares = _down(budget / leg.price)
            if shares <= 0:
                raise _Drop("malformed_idea", f"{leg.label}: rounds to zero shares")
            done[leg.label] = replace(leg, shares=shares, est_value=q4(shares * leg.price))
        return [done[leg.label] for leg in legs]

    # -- steps 5 and 6 ---------------------------------------------------------------------------------------------

    def _fit_caps(
        self,
        legs: list[_Leg],
        held: dict[str, Decimal],
        prices: dict[str, Decimal],
        nav: Decimal,
        cash_free: Decimal,
        bought: dict[str, Decimal],
        cfg: ProposalConfig,
    ) -> list[_Leg]:
        """Resize or drop buys that would breach the position cap or need more cash than is free (AS §8.2, ⚑AD24)."""
        for label in [leg.label for leg in legs if leg.action == "BUY"]:
            leg = next(x for x in legs if x.label == label)
            current = held.get(leg.ticker, Decimal(0)) * prices[leg.ticker] + bought.get(leg.ticker, Decimal(0))
            room = cfg.max_position_pct / HUNDRED * nav - current
            if leg.est_value > room:
                if room <= 0:
                    raise _Drop("position_cap", f"{leg.ticker} is already at or over the cap")
                legs = self._scale(legs, leg, room / leg.est_value, "position_cap", held)
                leg = next(x for x in legs if x.label == label)
                if leg.est_value > room:  # rounding left it over: fail closed
                    raise _Drop("position_cap", f"{leg.ticker} cannot be sized under the cap")
            if leg.kind == "target_value" and leg.est_value > cash_free:
                if cash_free <= 0:
                    raise _Drop("cash", "no free cash")
                legs = self._scale(legs, leg, cash_free / leg.est_value, "cash", held)
                leg = next(x for x in legs if x.label == label)
            if leg.kind == "target_value":
                cash_free -= leg.est_value
        return legs

    def _scale(
        self, legs: list[_Leg], buy: _Leg, factor: Decimal, reason: NotProposedReason, held: dict[str, Decimal]
    ) -> list[_Leg]:
        """Shrink ``buy`` by ``factor``: its own target value, or (for a pair) the sell fraction that funds it."""
        if buy.kind == "proceeds_of":
            scaled = [
                replace(x, fraction=_down((x.fraction or Decimal(0)) * factor)) if x.label == buy.source else x
                for x in legs
            ]
            if any(x.label == buy.source and (x.fraction or 0) <= 0 for x in scaled):
                raise _Drop(reason, "resized to zero")
        else:
            scaled = [replace(x, value=_down((x.value or Decimal(0)) * factor)) if x is buy else x for x in legs]
            if any(x.label == buy.label and (x.value or 0) <= 0 for x in scaled):
                raise _Drop(reason, "resized to zero")
        return self._estimate(scaled, held)

    @staticmethod
    def _check_oversell(legs: list[_Leg], held: dict[str, Decimal], sold: dict[str, Decimal]) -> None:
        """Across ideas, shares sold per ticker may not exceed the shares held (long-only)."""
        for leg in legs:
            if leg.action == "SELL" and sold.get(leg.ticker, Decimal(0)) + leg.shares > held[leg.ticker]:
                raise _Drop("short_or_oversell", f"{leg.ticker}: more than the whole position across ideas")

    # -- steps 7 and 8 ---------------------------------------------------------------------------------------------

    def _assign(
        self,
        accepted: list[tuple[TradeIdea, list[_Leg]]],
        intent: ReasonerOutput,
        cfg: ProposalConfig,
        created_at: datetime,
        existing_ids: Collection[str],
    ) -> list[ProposalDoc]:
        prefix = f"P-{created_at.date().isoformat()}-"
        used = [int(i[len(prefix) :]) for i in existing_ids if i.startswith(prefix) and i[len(prefix) :].isdigit()]
        seq = max(used, default=0)
        execute_at = self._calendar.next_session_open_at_or_after(
            add_elapsed(created_at, timedelta(hours=cfg.veto_window_hours))
        )
        execute_at = execute_at.astimezone(PT)
        expires_at = execute_at + timedelta(minutes=cfg.proposal_grace_minutes)
        caps = CapsSnapshot(
            max_position_pct=cfg.max_position_pct,
            new_position_max_pct=cfg.new_position_max_pct,
            proposal_sector_max_pct=cfg.proposal_sector_max_pct,
            max_trades_per_cycle=cfg.max_trades_per_cycle,
        )
        proposals: list[ProposalDoc] = []
        for idea, legs in accepted:
            seq += 1
            pid = proposal_id(created_at.date(), seq)
            pairs = {s: f"pair{n}" for n, s in enumerate(sorted({x.source for x in legs if x.source}), start=1)}
            doc_legs = [
                ProposalLeg(
                    leg=x.label,
                    trade_id=trade_id(pid, x.label),
                    pair=pairs.get(x.label) or pairs.get(x.source or ""),
                    action=x.action,
                    ticker=x.ticker,
                    size=ProposalSize(
                        kind=x.kind,
                        fraction=x.fraction,
                        source_leg=x.source,
                        target_value=x.value,
                    ),
                    est_shares=x.shares,
                    est_price=x.price,
                    est_value=x.est_value,
                    thesis=x.thesis,
                )
                for x in legs
            ]
            first = TransitionRecord(
                seq=1,
                from_state=None,
                to="PROPOSED",
                at=created_at,
                actor="system:evaluation",
                cause="created",
                prev_hash=GENESIS_HASH,
                hash="",
            )
            first = first.model_copy(
                update={"hash": chain_hash(GENESIS_HASH, first.model_dump(mode="json", by_alias=True))}
            )
            proposals.append(
                ProposalDoc(
                    proposal_id=pid,
                    created_at=created_at,
                    state="PROPOSED",
                    approval_mode="auto" if cfg.auto_approve else "explicit",
                    veto_window_hours=cfg.veto_window_hours,
                    fill_session_date=execute_at.date(),
                    execute_at=execute_at,
                    expires_at=expires_at,
                    caps=caps,
                    legs=doc_legs,
                    rationale=_sanitise(idea.rationale, RATIONALE_MAX_CHARS),
                    hold_alternative=_sanitise(
                        f"{idea.why_beats_hold} {intent.hold_alternative}", HOLD_ALTERNATIVE_MAX_CHARS
                    ),
                    transitions=[first],
                )
            )
        return proposals
