"""The ``triage.*`` settings (v2 design D1): the news triage tier. File-edit only, never reachable via ``SET`` (R1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TriageConfig(BaseModel):
    """Settings for the triage cron job. Strict defaults: off, and log-only even once switched on.

    Escalation needs ``enabled`` AND ``shadow_mode: false`` AND ``benchmark_passed: true`` (``escalation_live``): the
    >=4-week shadow run and the Q1 benchmark gate production enable (test plan §4), so one flag cannot do it alone.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    shadow_mode: bool = True
    benchmark_passed: bool = False
    confidence_threshold: float = Field(default=0.85, ge=0, le=1)
    #: ``HH:MM-HH:MM`` in PT, may wrap midnight. Nothing pages inside it (R5).
    quiet_hours: str = Field(default="22:00-06:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")
    max_escalations_per_day: int = Field(default=3, ge=1, le=50)
    #: At most 24: the dedupe ledger is shared with the outbox, whose claims prune entries older than 24 hours.
    dedupe_window_hours: int = Field(default=24, ge=1, le=24)
    max_items_per_run: int = Field(default=20, ge=1, le=200)

    @property
    def escalation_live(self) -> bool:
        """True only when escalation may leave the log: enabled, out of shadow mode, benchmark passed."""
        return self.enabled and not self.shadow_mode and self.benchmark_passed
