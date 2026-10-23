"""The ``llm.*`` reasoner settings (AS §3.6, LP §4.1). Operational: file-edit only, never reachable via ``SET``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LLMConfig(BaseModel):
    """Reasoner call settings. ``extractor_model`` lives in ``NewsConfig``."""

    model_config = ConfigDict(extra="forbid")

    reasoner_model: str = "claude-sonnet-5-5"
    reasoner_max_tokens: int = Field(default=32000, ge=1)
    reasoner_effort: str = "high"
    reasoner_timeout_s: int = Field(default=120, ge=1)
    reasoner_max_retries: int = Field(default=2, ge=0)
