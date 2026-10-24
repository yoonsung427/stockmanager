"""Proposal alert buttons (v2 H4): the VETO / APPROVE message components and their ``custom_id`` codec.

Pure and transport-free: ``notify.deliver.render`` attaches ``proposal_components`` to a Discord proposal embed, and
the bot decodes a click with ``parse_custom_id`` into the same command text a user would type. Nothing here knows
about authorization; a click is authorized by ``commands.auth.authorize`` like any typed command.

Persistence (the "bot down" ruling): a ``custom_id`` carries everything needed to resolve a click (action and
proposal ID), and no view object, timeout or in-memory registry is involved. The buttons therefore never expire and
work again the moment a bot process is running, whatever restarted in between. A click made while no bot is
running is *not* queued: Discord answers the clicker with its own "interaction failed" notice (an interaction token
lives 3 s and cannot be replayed), the proposal is untouched, and the embed's typed-command block and the CLI remain
the fallback. See ``bot.bot.PortfolioBot.handle_component``.
"""

from __future__ import annotations

import re
from typing import Any, Literal

ComponentAction = Literal["VETO", "APPROVE"]

COMPONENT_PREFIX = "sm"

_BUTTON_STYLE_SUCCESS = 3
_BUTTON_STYLE_DANGER = 4
_PROPOSAL_ID = r"P-\d{4}-\d{2}-\d{2}-\d{2}"  # same shape as ``commands.parser.PROPOSAL_ID_RE`` (not imported: cycle)
_CUSTOM_ID_RE = re.compile(rf"^{COMPONENT_PREFIX}:(VETO|APPROVE):({_PROPOSAL_ID})$")


def custom_id(action: ComponentAction, proposal_id: str) -> str:
    """The ``custom_id`` of ``action``'s button on ``proposal_id`` (Discord caps it at 100 characters)."""
    return f"{COMPONENT_PREFIX}:{action}:{proposal_id}"


def parse_custom_id(value: str) -> tuple[ComponentAction, str] | None:
    """Decode a button ``custom_id``; ``None`` for anything that is not one of ours."""
    match = _CUSTOM_ID_RE.fullmatch(value)
    if match is None:
        return None
    action: ComponentAction = "VETO" if match.group(1) == "VETO" else "APPROVE"
    return action, match.group(2)


def proposal_components(proposal_id: str) -> list[dict[str, Any]]:
    """One action row with the VETO and APPROVE buttons for ``proposal_id`` (Discord component JSON)."""
    if re.fullmatch(_PROPOSAL_ID, proposal_id) is None:
        return []
    return [
        {
            "type": 1,
            "components": [
                {"type": 2, "style": _BUTTON_STYLE_DANGER, "label": "Veto", "custom_id": custom_id("VETO", proposal_id)},
                {
                    "type": 2,
                    "style": _BUTTON_STYLE_SUCCESS,
                    "label": "Approve",
                    "custom_id": custom_id("APPROVE", proposal_id),
                },
            ],
        }
    ]


def click_command(action: ComponentAction, proposal_id: str, reason: str | None = None) -> str:
    """The typed-command text a click stands for, so it resolves through the very same handler (``commands.handle``)."""
    text = f"{action} {proposal_id}"
    if action == "VETO" and reason and reason.strip():
        text += f" {reason}"
    return text
