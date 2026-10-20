"""The slice of ``config.yaml`` the command layer and Discord bot read (AS §3.6, §11.3).

This is intentionally a subset of the full configuration surface described in the architecture spec: enough to
drive authorization, ``SET`` and the notification catalogue. ``ops.*``, ``llm.*`` and ``news.*`` stay as their own
config models (``config/proposals.py``, ``config/llm.py``) and are never reachable through ``SET``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from stockmanager.config.llm import LLMConfig
from stockmanager.config.proposals import ProposalConfig

PriceSource = Literal["yahoo", "finnhub"]


class DiscordConfig(BaseModel):
    """Discord identifiers (AS §3.6): file-edit only, never reachable via ``SET``."""

    model_config = ConfigDict(extra="forbid")

    allowed_user_ids: list[str] = Field(default_factory=list)
    channel_id: str | None = None


class OpsConfig(BaseModel):
    """Operational settings (AS §3.6): file-edit only, never reachable via ``SET``."""

    model_config = ConfigDict(extra="forbid")

    exec_delay_minutes: int = Field(default=5, ge=1)
    late_threshold_minutes: int = Field(default=10, ge=1)
    lock_stale_minutes: int = Field(default=15, ge=1)


class NewsOpsConfig(BaseModel):
    """News operational settings (AS §3.6): file-edit only, never reachable via ``SET``."""

    model_config = ConfigDict(extra="forbid")

    max_items_per_query: int = Field(default=10, ge=1)
    max_facts_per_ticker: int = Field(default=15, ge=1)


class Config(BaseModel):
    """The configuration the command layer reads (AS §3.6). ``SET`` validates and mutates a copy of this."""

    model_config = ConfigDict(extra="forbid")

    start_cash: Decimal = Field(default=Decimal(50000), gt=0)
    auto_approve: bool = True
    veto_window_hours: int = Field(default=4, ge=1, le=168)
    evaluation_frequency: Literal["daily", "weekly", "biweekly"] = "weekly"
    evaluation_day: Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"] = "saturday"
    evaluation_time: str = Field(default="09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    max_position_pct: Decimal = Field(default=Decimal(30), ge=1, le=100)
    position_alert_pct: Decimal = Field(default=Decimal(40), ge=1, le=100)
    max_trades_per_cycle: int = Field(default=3, ge=1, le=10)
    proposal_grace_minutes: int = Field(default=60, ge=10, le=240)
    max_sector_pct: int = Field(default=80, ge=1, le=100)
    drawdown_alert: Decimal = Field(default=Decimal(5), gt=0, le=100)
    benchmark: str = "SPY"
    price_source_primary: PriceSource = "yahoo"
    price_source_fallback: PriceSource = "finnhub"
    risk_free_rate_annual: Decimal = Field(default=Decimal("0.0"), ge=0, le=Decimal("0.2"))
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    ops: OpsConfig = Field(default_factory=OpsConfig)
    news: NewsOpsConfig = Field(default_factory=NewsOpsConfig)
    proposals: ProposalConfig = Field(default_factory=ProposalConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)


# -- SET (AS §3.6 table): every key a Discord/CLI SET command may change -------------------------------------------

SETTABLE_KEYS: frozenset[str] = frozenset(
    {
        "auto_approve",
        "veto_window_hours",
        "evaluation_frequency",
        "evaluation_day",
        "evaluation_time",
        "max_position_pct",
        "position_alert_pct",
        "max_trades_per_cycle",
        "proposal_grace_minutes",
        "max_sector_pct",
        "drawdown_alert",
        "risk_free_rate_annual",
        "price_source_primary",
        "price_source_fallback",
    }
)

# Namespaces that are file-edit only and must never be reachable via SET, even with a dotted key.
BLOCKED_SET_PREFIXES: tuple[str, ...] = ("ops.", "llm.", "news.", "discord.")
