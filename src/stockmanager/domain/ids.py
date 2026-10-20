"""ID construction and parsing."""

from __future__ import annotations

from datetime import date


def opening_proposal_id(session_date: date) -> str:
    """Proposal ID used for opening fills, e.g. ``P-OPEN-2026-10-12``."""
    return f"P-OPEN-{session_date.isoformat()}"


def trade_id(proposal_id: str, leg: str) -> str:
    """Trade ID of the form ``<proposal_id>-<leg>``, e.g. ``P-OPEN-2026-10-12-buy1``."""
    return f"{proposal_id}-{leg}"


def proposal_id(created: date, seq: int) -> str:
    """Proposal ID ``P-YYYY-MM-DD-NN`` (date = creation date in PT, ``NN`` zero-padded sequence), AS §3.3."""
    return f"P-{created.isoformat()}-{seq:02d}"
