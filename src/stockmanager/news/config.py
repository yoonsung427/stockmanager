"""The ``news`` / ``llm.extractor_model`` slice of the configuration (AS §3.6, news-sources §2.1).

Operational settings: file-edit only, never reachable via ``SET``.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

QueryKind = Literal["holding", "analyst", "earnings", "benchmark", "sector", "macro"]
ALL_KINDS: tuple[QueryKind, ...] = ("holding", "analyst", "earnings", "benchmark", "sector", "macro")
TICKER_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")


class ExtractorConfig(BaseModel):
    """Per-item extraction call settings (AS §7.4)."""

    model_config = ConfigDict(extra="forbid")

    concurrency: int = Field(default=8, ge=1, le=8)
    timeout_s: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    max_tokens: int = Field(default=600, ge=1)


class SourceConfig(BaseModel):
    """One adapter's concurrency, pacing and the query kinds it may serve."""

    model_config = ConfigDict(extra="forbid")

    max_concurrency: int = Field(default=1, ge=1)
    kinds: list[QueryKind]
    rate_per_min: float | None = Field(default=None, gt=0)
    min_interval_ms: int | None = Field(default=None, ge=0)


def _default_sources() -> dict[str, SourceConfig]:
    return {
        "searxng": SourceConfig(max_concurrency=4, min_interval_ms=100, kinds=list(ALL_KINDS)),
        "finnhub": SourceConfig(max_concurrency=2, rate_per_min=25, kinds=["holding", "analyst", "earnings", "macro"]),
        "yahoo_rss": SourceConfig(max_concurrency=1, rate_per_min=30, kinds=["holding"]),
    }


class NewsConfig(BaseModel):
    """Everything the news stage reads from configuration."""

    model_config = ConfigDict(extra="forbid")

    benchmark: str = "SPY"
    benchmark_name: str | None = None
    sector_etfs: list[str] = Field(
        default_factory=lambda: ["XLK", "XLF", "XLV", "XLE", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC"]
    )
    universe: list[str] = Field(default_factory=list)  # configured tickers the model may name beyond holdings
    extractor_model: str = "claude-haiku-5-5"

    max_items_per_query: int = Field(default=10, ge=1)
    max_facts_per_ticker: int = Field(default=15, ge=1)
    fallback_chain: list[str] = Field(default_factory=lambda: ["searxng", "finnhub", "yahoo_rss"])
    lookback_days: int = Field(default=7, ge=1, le=60)  # hard max 60 = the fact date floor (AS §7.5)
    macro_cadence_days: int = Field(default=7, ge=1)
    earnings_horizon_days: int = Field(default=30, ge=1)
    max_items_per_cycle: int = Field(default=200, ge=1)
    max_facts_per_ticker_domain: int = Field(default=3, ge=1)
    max_facts_total: int = Field(default=150, ge=1)
    title_max_chars: int = Field(default=300, ge=1)
    snippet_max_chars: int = Field(default=2000, ge=1)
    stale_feed_days: int = Field(default=4, ge=1)
    fetch_stage_deadline_s: float = Field(default=150.0, gt=0)
    extract_stage_deadline_s: float = Field(default=300.0, gt=0)
    extractor: ExtractorConfig = Field(default_factory=ExtractorConfig)
    sources: dict[str, SourceConfig] = Field(default_factory=_default_sources)
    breaker_failures_to_open: int = Field(default=3, ge=1)

    @field_validator("benchmark")
    @classmethod
    def _benchmark_syntax(cls, v: str) -> str:
        if not TICKER_RE.fullmatch(v):
            raise ValueError(f"bad ticker {v!r}")
        return v

    @field_validator("sector_etfs", "universe")
    @classmethod
    def _ticker_syntax(cls, v: list[str]) -> list[str]:
        for t in v:
            if not TICKER_RE.fullmatch(t):
                raise ValueError(f"bad ticker {t!r}")
        return v

    @model_validator(mode="after")
    def _cross_field(self) -> NewsConfig:
        if self.max_items_per_cycle < self.max_items_per_query:
            raise ValueError("max_items_per_cycle must be >= max_items_per_query")
        for name in self.fallback_chain:
            if name not in self.sources:
                raise ValueError(f"fallback_chain names {name!r} but sources has no entry for it")
        covered = {k for name in self.fallback_chain for k in self.sources[name].kinds}
        missing = [k for k in ALL_KINDS if k not in covered]
        if missing:
            raise ValueError(f"no adapter in fallback_chain serves query kinds {missing}")
        return self

    def relevance_set(self, holdings: list[str]) -> frozenset[str]:
        """Tickers an extracted fact may name: holdings, benchmark, sector ETFs and the configured universe."""
        return frozenset([*holdings, self.benchmark, *self.sector_etfs, *self.universe])
