"""The ``summary.*`` settings: AI stock summaries (digest, price-threshold alert, Discord query). File-edit only."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SummaryConfig(BaseModel):
    """Settings for the summary generator, which talks to an OpenAI-compatible server (LM Studio), not Anthropic."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    digest_at_close: bool = True
    alert_on_threshold: bool = True
    price_threshold_pct: float = Field(default=5.0, gt=0, le=100)
    model: str = "mistralai/mistral-7b-instruct-v0.3"
    base_url: str = "http://localhost:1234/v1"
    max_tokens: int = Field(default=4000, ge=1)
    temperature: float = Field(default=0.3, ge=0, le=2)
    timeout_s: int = Field(default=60, ge=1)
