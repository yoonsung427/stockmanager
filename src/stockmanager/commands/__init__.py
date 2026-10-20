"""The command layer (AS §1.7): parsing, authorization and handlers shared by Discord and the CLI."""

from __future__ import annotations

from stockmanager.commands.auth import AuthorizationResult, Sender, authorize
from stockmanager.commands.handlers import CommandContext, handle
from stockmanager.commands.parser import ParsedCommand, parse_command

__all__ = [
    "AuthorizationResult",
    "CommandContext",
    "ParsedCommand",
    "Sender",
    "authorize",
    "handle",
    "parse_command",
]
