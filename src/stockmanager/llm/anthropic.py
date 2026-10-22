"""Anthropic implementation of ``LLMBackend``: adaptive thinking, explicit effort, JSON-schema output, streaming."""

from __future__ import annotations

import logging
from typing import Any, Literal

import anthropic

from stockmanager.config.llm import LLMConfig
from stockmanager.config.proposals import ProposalConfig
from stockmanager.llm.protocol import ChatResult, LLMAuthError, LLMError, LLMTimeout

log = logging.getLogger(__name__)

_STOP_REASONS = {
    "refusal": "refusal",
    "max_tokens": "max_tokens",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}


class AnthropicLLMBackend:
    """Calls the Anthropic Messages API. No tools, no web.

    The defaults are the Reasoner's call: streamed, adaptive thinking, ``config.reasoner_effort``. The Extractor
    builds its own instance with ``thinking="disabled"``, ``effort="low"`` and ``stream=False``. ``limits`` is accepted
    for parity with ``AnthropicReasoner`` (prompt rendering uses it, the transport does not).
    """

    def __init__(
        self,
        config: LLMConfig,
        limits: ProposalConfig | None = None,
        *,
        thinking: Literal["adaptive", "disabled"] = "adaptive",
        effort: str | None = None,
        stream: bool = True,
        max_retries: int | None = None,
        client: Any = None,
    ) -> None:
        self._thinking = thinking
        self._effort = effort if effort is not None else config.reasoner_effort
        self._stream = stream
        # The SDK retries 408/409/429/5xx and timeouts itself.
        self._client: Any = client or anthropic.Anthropic(
            max_retries=config.reasoner_max_retries if max_retries is None else max_retries,
            timeout=float(config.reasoner_timeout_s),
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
        output_config: dict[str, Any] = {"effort": self._effort}
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "thinking": {"type": self._thinking},
            "output_config": output_config,
            "timeout": timeout,
        }
        try:
            if self._stream:
                with self._client.messages.stream(**kwargs) as stream:
                    resp = stream.get_final_message()
            else:
                resp = self._client.messages.create(**kwargs)
        except anthropic.APITimeoutError as e:
            raise LLMTimeout(str(e)) from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise LLMAuthError(str(e)) from e
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            raise LLMError(str(e)) from e
        except Exception as e:  # missing or unresolvable credentials surface as TypeError / AnthropicError
            raise LLMAuthError(str(e)) from e
        raw_stop = str(resp.stop_reason)
        stop = _STOP_REASONS.get(raw_stop)
        if stop is None:
            log.warning("unmapped anthropic stop_reason %r treated as 'stop'", raw_stop)
            stop = "stop"
        resp_model = getattr(resp, "model", None)
        text = "".join(b.text for b in resp.content if b.type == "text")
        return ChatResult(text=text, stop_reason=stop, model_id=resp_model if isinstance(resp_model, str) else model)
