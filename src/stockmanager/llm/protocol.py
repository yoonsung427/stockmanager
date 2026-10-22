"""The ``LLMBackend`` port: one system+user call in, text and a normalised stop reason out.

Backends translate their provider's stop reasons to ``STOP_REASONS`` and their provider's exceptions to the
``LLMError`` family, so callers never import a provider SDK.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

#: The normalised ``ChatResult.stop_reason`` values.
STOP_REASONS = ("refusal", "max_tokens", "stop", "tool_calls")


class LLMError(Exception):
    """The call failed: connection, 5xx, 429 or any other non-timeout, non-auth failure."""


class LLMTimeout(LLMError):
    """The call timed out."""


class LLMAuthError(LLMError):
    """Credentials are missing, unresolvable or rejected (401/403)."""


@dataclass(frozen=True)
class ChatResult:
    """A completed call. ``stop_reason`` is one of ``STOP_REASONS``; ``model_id`` is the model that answered."""

    text: str
    stop_reason: str
    model_id: str


class LLMBackend(Protocol):
    """Any chat-completion provider. Raises ``LLMError`` (``LLMTimeout``, ``LLMAuthError``) on failure."""

    def chat_completion(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        json_schema: dict[str, Any] | None = None,
        timeout: float,
    ) -> ChatResult:
        """Run one call. ``json_schema`` asks for structured JSON output where the backend supports it."""
        ...
