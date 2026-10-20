"""The slice of ``config.yaml`` the proposal builder and the Reasoner's system prompt read (AS §3.6, §8.2, LP §5)."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ProposalConfig(BaseModel):
    """Limits, approval mode and timing for new proposals, read once per cycle from one config snapshot."""

    model_config = ConfigDict(extra="forbid")

    max_position_pct: Decimal = Field(default=Decimal(30), ge=1, le=100)
    new_position_max_pct: int = Field(default=10, ge=1, le=100)  # LP §6.1 AD26
    proposal_sector_max_pct: int = Field(default=80, ge=1, le=100)  # LP §6.1 AD26
    max_trades_per_cycle: int = Field(default=3, ge=1, le=10)
    auto_approve: bool = True
    veto_window_hours: int = Field(default=4, ge=1, le=168)
    proposal_grace_minutes: int = Field(default=60, ge=10, le=240)
