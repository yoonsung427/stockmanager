"""Pydantic document models persisted as canonical JSON."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stockmanager.domain.money import q4
from stockmanager.pricing.errors import MixedSourceError

PriceSource = Literal["yahoo", "finnhub"]
Status = Literal["STAGED", "APPROVED", "LIVE", "PAUSED", "CLOSED"]


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
