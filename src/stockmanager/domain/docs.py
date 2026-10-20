"""Pydantic document models persisted as canonical JSON."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stockmanager.domain.money import q4
from stockmanager.domain.proposal_state import ProposalState, chain_hash, require_transition
from stockmanager.pricing.errors import MixedSourceError

PriceSource = Literal["yahoo", "finnhub"]
Status = Literal["STAGED", "APPROVED", "LIVE", "PAUSED", "CLOSED"]
NewsEvent = Literal["earnings", "guidance", "rating_change", "regulatory", "product", "m&a", "macro", "other"]


class _Doc(BaseModel):
    """Base for all documents: unknown fields are errors, types are strict."""

    model_config = ConfigDict(extra="forbid", strict=True)


class Quote(_Doc):
    """A single price observation from one provider."""

    ticker: str
    price: Decimal = Field(gt=0)
    source: PriceSource
    timestamp: datetime

    @field_validator("price")
    @classmethod
    def _price_4dp(cls, v: Decimal) -> Decimal:
        return q4(v)


@dataclass(frozen=True)
class PriceQuote:
    """The price of one ticker for one session (``open`` or ``close``) from one provider (AS §6.1).

    ``price`` is 4 dp by construction (adapters pass it through ``q4``); ``fetched_at`` is injected-clock time.
    """

    ticker: str
    session_date: date
    kind: Literal["open", "close"]
    price: Decimal
    source: PriceSource
    fetched_at: datetime


class PriceTable(_Doc):
    """Quotes for one valuation: one source, one session. Mixed tables cannot be constructed (TS EDGE-31)."""

    source: PriceSource
    session_date: date
    quotes: dict[str, PriceQuote]

    @model_validator(mode="after")
    def _single_source_and_session(self) -> PriceTable:
        for ticker, quote in self.quotes.items():
            if quote.source != self.source:
                raise MixedSourceError(f"{ticker}: source {quote.source!r} in a {self.source!r} table")
            if quote.session_date != self.session_date:
                raise MixedSourceError(f"{ticker}: session {quote.session_date} in a {self.session_date} table")
            if quote.ticker != ticker:
                raise ValueError(f"quote for {quote.ticker} filed under {ticker}")
        return self


class Position(_Doc):
    """An open holding. ``positions[]`` is a derived view of ``trades[]``."""

    ticker: str
    shares: Decimal = Field(gt=0)
    fill_price: Decimal = Field(gt=0)
    fill_time: datetime
    cost_basis: Decimal = Field(ge=0)
    thesis: str
    price_source: PriceSource

    @field_validator("shares", "fill_price", "cost_basis")
    @classmethod
    def _4dp(cls, v: Decimal) -> Decimal:
        return q4(v)


class Trade(_Doc):
    """An immutable record of one fill."""

    trade_id: str
    timestamp: datetime
    action: Literal["BUY", "SELL"]
    ticker: str
    shares: Decimal = Field(gt=0)
    fill_price: Decimal = Field(gt=0)
    cost: Decimal = Field(ge=0)
    price_source: PriceSource
    thesis: str = ""

    @field_validator("shares", "fill_price", "cost")
    @classmethod
    def _4dp(cls, v: Decimal) -> Decimal:
        return q4(v)


class PortfolioDoc(_Doc):
    """``portfolio.json``: the single source of truth (AS §3.1)."""

    schema_version: int = Field(default=1, ge=1)
    rev: int = Field(default=0, ge=0)
    updated_at: datetime | None = None
    start_cash: Decimal = Field(ge=0)
    cash: Decimal = Field(ge=0)
    status: Status
    positions: list[Position] = Field(default_factory=list)
    trades: list[Trade] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    closed_at: datetime | None = None
    final_report: dict[str, Any] | None = None

    @field_validator("start_cash", "cash")
    @classmethod
    def _4dp(cls, v: Decimal) -> Decimal:
        return q4(v)

    @field_validator("trades")
    @classmethod
    def _unique_trade_ids(cls, v: list[Trade]) -> list[Trade]:
        ids = [t.trade_id for t in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate trade_id")
        return v

    def to_canonical_json(self) -> str:
        """Serialize with indent=2, ensure_ascii=False and a trailing newline."""
        return json.dumps(self.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"


class Fact(_Doc):
    """An extracted, validated news fact (AS §3.8). There is deliberately no price field (SS §6, TS INJ-06)."""

    fact_id: str
    ticker: str
    event: NewsEvent
    description: str
    date: dt.date
    source_url: str
    source_domain: str
    display_title: str  # dashboard only; excluded from for_reasoner()

    def for_reasoner(self) -> dict[str, Any]:
        """Projection for ``ReasonerInput`` (AS §7.3): excludes ``display_title`` and ``source_url``."""
        return {
            "fact_id": self.fact_id,
            "ticker": self.ticker,
            "event": self.event,
            "description": self.description,
            "date": self.date,
            "source_domain": self.source_domain,
        }


class ProposalSize(_Doc):
    """A leg's sizing rule (AS §3.3). Which fields are set depends on ``kind``; the others stay ``None``."""

    kind: Literal["fraction_of_position", "proceeds_of", "target_value"]
    fraction: Decimal | None = None  # fraction_of_position, proceeds_of
    source_leg: str | None = None  # proceeds_of: the SELL leg whose proceeds fund this buy
    target_value: Decimal | None = None  # target_value: dollars drawn from cash

    @field_validator("fraction", "target_value")
    @classmethod
    def _4dp(cls, v: Decimal | None) -> Decimal | None:
        return None if v is None else q4(v)

    @model_validator(mode="after")
    def _kind_fields(self) -> ProposalSize:
        have = {k for k in ("fraction", "source_leg", "target_value") if getattr(self, k) is not None}
        want = {
            "fraction_of_position": {"fraction"},
            "proceeds_of": {"fraction", "source_leg"},
            "target_value": {"target_value"},
        }[self.kind]
        if have != want:
            raise ValueError(f"{self.kind} needs exactly {sorted(want)}, got {sorted(have)}")
        return self


class ProposalLeg(_Doc):
    """One leg of a proposal. Estimates are at valuation prices; execution recomputes at actual fill prices."""

    leg: str  # "sell1", "buy1", ...
    trade_id: str  # <proposal_id>-<leg>
    pair: str | None = None  # legs funded by each other share a group
    action: Literal["BUY", "SELL"]
    ticker: str
    size: ProposalSize
    est_shares: Decimal = Field(gt=0)
    est_price: Decimal = Field(gt=0)
    est_value: Decimal = Field(gt=0)
    thesis: str = ""  # buys only

    @field_validator("est_shares", "est_price", "est_value")
    @classmethod
    def _4dp(cls, v: Decimal) -> Decimal:
        return q4(v)


class CapsSnapshot(_Doc):
    """The limits in force when the proposal was created (AS §3.3, LP §6.1)."""

    max_position_pct: Decimal
    new_position_max_pct: int
    proposal_sector_max_pct: int
    max_trades_per_cycle: int


class TransitionRecord(_Doc):
    """One entry of the append-only, hash-chained ``transitions[]`` (AS §3.3, ⚑AD19)."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    seq: int = Field(ge=1)
    from_state: ProposalState | None = Field(alias="from")
    to: ProposalState
    at: datetime
    actor: str
    cause: str
    prev_hash: str
    hash: str


class DeliveryReceipt(_Doc):
    """A single delivery receipt, appended when a veto alert reaches a visible channel (AS §8.3)."""

    channel: str
    delivered_at: datetime
    veto_deadline: datetime


class ProposalDelivery(_Doc):
    """Receipts from visible-channel delivery (AS §8.3); empty until the veto alert has reached the user."""

    receipts: list[DeliveryReceipt] = Field(default_factory=list)


class ProposalDoc(_Doc):
    """``proposals/<proposal_id>.json`` (AS §3.3): one idea's legs, timing and audit trail."""

    schema_version: int = Field(default=1, ge=1)
    proposal_id: str
    created_at: datetime
    state: ProposalState
    approval_mode: Literal["auto", "explicit"]
    veto_window_hours: int = Field(ge=1)
    veto_deadline: datetime | None = None  # bound at delivery (AS §8.3), never at creation
    fill_session_date: dt.date
    execute_at: datetime
    expires_at: datetime
    caps: CapsSnapshot
    legs: list[ProposalLeg] = Field(min_length=1)
    rationale: str
    hold_alternative: str
    reconfirms: str | None = None  # set at creation by RECONFIRM: the EXPIRED proposal this one replaces (AS §8.7)
    reconfirmed_as: str | None = None  # set once on the EXPIRED original when RECONFIRM succeeds; makes it idempotent
    reconfirm_check: Literal["pending", "passed", "failed"] | None = None  # None unless created by RECONFIRM
    delivery: ProposalDelivery = Field(default_factory=ProposalDelivery)  # absent in older files: no receipts
    transitions: list[TransitionRecord] = Field(min_length=1)

    def to_canonical_json(self) -> str:
        """Serialize with indent=2, ensure_ascii=False and a trailing newline; transitions use ``from``."""
        return json.dumps(self.model_dump(mode="json", by_alias=True), indent=2, ensure_ascii=False) + "\n"

    def transition(self, to: ProposalState, actor: str, cause: str, at: datetime) -> ProposalDoc:
        """This proposal after one legal transition (AS §8.4), with the new hash-chained ``transitions[]`` entry.

        Raises ``IllegalTransition`` if ``self.state -> to`` is not in the table.
        """
        require_transition(self.state, to)
        prev = self.transitions[-1]
        rec = TransitionRecord(
            seq=prev.seq + 1, from_state=self.state, to=to, at=at, actor=actor, cause=cause, prev_hash=prev.hash, hash=""
        )
        rec = rec.model_copy(update={"hash": chain_hash(prev.hash, rec.model_dump(mode="json", by_alias=True))})
        return self.model_copy(update={"state": to, "transitions": [*self.transitions, rec]})


NotProposedReason = Literal[
    "invalid_symbol",
    "short_or_oversell",
    "malformed_idea",
    "pairing",
    "unpriced",
    "trade_cap",
    "position_cap",
    "cash",
]


class NotProposed(_Doc):
    """An idea the builder did not turn into a proposal; recorded under ``ideas_not_proposed`` (AS §7.7)."""

    priority: int
    reason: NotProposedReason
    detail: str = ""
