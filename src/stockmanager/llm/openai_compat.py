"""OpenAI-compatible implementation of ``LLMBackend`` (OpenAI, Ollama, vLLM, ...). The ``openai`` package is optional."""

from __future__ import annotations

import importlib
import logging
from typing import Any

from stockmanager.llm.protocol import ChatResult, LLMAuthError, LLMError, LLMTimeout

log = logging.getLogger(__name__)

UNAVAILABLE_MODEL_ID = "openai_unavailable"

_FINISH_REASONS = {
    "stop": "stop",
    "length": "max_tokens",
    "content_filter": "refusal",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
}


class OpenAILLMBackend:
    """Calls ``chat.completions`` on ``base_url``. JSON mode only: thinking and effort are Anthropic-specific.

    If the ``openai`` package is not installed the backend degrades instead of crashing: every call returns a
    ``"refusal"`` result with ``model_id="openai_unavailable"`` and a warning is logged.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "dummy",
        timeout: float = 120.0,
        max_retries: int = 2,
        *,
        client: Any = None,
    ) -> None:
        self._openai: Any = None
        self._client: Any = client
        try:
            self._openai = importlib.import_module("openai")
        except ImportError:
            if client is None:
                log.warning("the 'openai' package is not installed: the OpenAI-compatible backend is unavailable")
            return
        if client is not None:
            return
        self._client = self._openai.OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=max_retries)
        log.info(
            "openai-compatible backend at %s: thinking and effort are Anthropic-specific and are skipped", base_url
        )

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
        """One call. Raises ``LLMTimeout``, ``LLMAuthError`` or ``LLMError``."""
        if self._client is None:
            return ChatResult(text="", stop_reason="refusal", model_id=UNAVAILABLE_MODEL_ID)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "timeout": timeout,
        }
        if json_schema is not None:
            # JSON mode guarantees valid JSON, not this schema; the caller's parser still validates the shape.
            kwargs["response_format"] = {"type": "json_object"}
            log.debug("json_schema is not enforced by json_object mode; thinking/effort skipped")
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as e:
            raise self._translate(e) from e
        choice = resp.choices[0]
        message = choice.message
        stop = "refusal" if getattr(message, "refusal", None) else _FINISH_REASONS.get(str(choice.finish_reason))
        if stop is None:
            log.warning("unmapped openai finish_reason %r treated as 'stop'", choice.finish_reason)
            stop = "stop"
        resp_model = getattr(resp, "model", None)
        return ChatResult(
            text=message.content or "", stop_reason=stop, model_id=resp_model if isinstance(resp_model, str) else model
        )

    def _translate(self, e: Exception) -> LLMError:
        def is_a(name: str) -> bool:
            cls = getattr(self._openai, name, None)
            return isinstance(cls, type) and isinstance(e, cls)

        if is_a("APITimeoutError"):
            return LLMTimeout(str(e))
        if is_a("AuthenticationError") or is_a("PermissionDeniedError"):
            return LLMAuthError(str(e))
        if is_a("APIStatusError") or is_a("APIConnectionError"):
            return LLMError(str(e))
        return LLMAuthError(str(e))  # same default as the Anthropic backend: unresolvable credentials
