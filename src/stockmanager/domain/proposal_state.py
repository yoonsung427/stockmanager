"""Proposal state machine (AS §8.4): the legal-transition table and the hash chain over ``transitions[]``."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

ProposalState = Literal[
    "PROPOSED",
    "VETO_WINDOW",
    "APPROVED",
    "EXECUTED",
    "VETOED",
    "EXPIRED",
    "CANCELLED_PAUSED",
    "CANCELLED_CLOSED",
    "VOIDED",
]

TERMINAL_STATES: frozenset[ProposalState] = frozenset(
    {"EXECUTED", "VETOED", "CANCELLED_PAUSED", "CANCELLED_CLOSED", "EXPIRED", "VOIDED"}
)

LEGAL_TRANSITIONS: frozenset[tuple[ProposalState, ProposalState]] = frozenset(
    {
        ("PROPOSED", "VETO_WINDOW"),
        ("PROPOSED", "VETOED"),
        ("PROPOSED", "CANCELLED_PAUSED"),
        ("PROPOSED", "CANCELLED_CLOSED"),
        ("PROPOSED", "EXPIRED"),
        ("VETO_WINDOW", "APPROVED"),
        ("VETO_WINDOW", "VETOED"),
        ("VETO_WINDOW", "CANCELLED_PAUSED"),
        ("VETO_WINDOW", "CANCELLED_CLOSED"),
        ("VETO_WINDOW", "EXPIRED"),
        ("APPROVED", "EXECUTED"),
        ("APPROVED", "VETOED"),
        ("APPROVED", "CANCELLED_PAUSED"),
        ("APPROVED", "CANCELLED_CLOSED"),
        ("APPROVED", "EXPIRED"),
        ("APPROVED", "VOIDED"),
    }
)

GENESIS_HASH = "0"


class IllegalTransition(Exception):
    """A transition not in ``LEGAL_TRANSITIONS`` (or out of a terminal state)."""


def is_terminal(state: ProposalState) -> bool:
    """True for the six end states; nothing leaves them (``RESUME`` never revives, ``RECONFIRM`` makes a new one)."""
    return state in TERMINAL_STATES


def can_transition(from_state: ProposalState, to_state: ProposalState) -> bool:
    """True if ``from_state -> to_state`` is in the legal table."""
    return (from_state, to_state) in LEGAL_TRANSITIONS


def require_transition(from_state: ProposalState, to_state: ProposalState) -> None:
    """Raise ``IllegalTransition`` unless ``from_state -> to_state`` is legal."""
    if not can_transition(from_state, to_state):
        raise IllegalTransition(f"{from_state} -> {to_state}")


def chain_hash(prev_hash: str, record: dict[str, Any]) -> str:
    """``sha256(prev_hash || canonical_json(record without hash))`` (AS §3.3, ⚑AD19). ``record`` is JSON-mode, aliased."""
    body = {k: v for k, v in record.items() if k != "hash"}
    canonical = json.dumps(body, indent=2, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()
