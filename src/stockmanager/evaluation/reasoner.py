"""The Reasoner (LLM seam 2, AS §8.1, LP §4): facts, prices and portfolio state in, trade *intent* out.

One streamed call per cycle with structured output, adaptive thinking and explicit effort. Any failure (refusal,
truncation, timeout, schema) raises ``ReasonerFailure``: the cycle then has no proposals (fail closed, AS §5.2).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, Protocol, Self, cast

import anthropic
from pydantic import ValidationError

from stockmanager.config.llm import LLMConfig
from stockmanager.config.proposals import ProposalConfig
from stockmanager.evaluation.prompts import (
    ReasonerPrompt,
    ReasonerUnavailable,
    load_reasoner_prompt,
)
from stockmanager.evaluation.reasoner_io import ReasonerInput, ReasonerOutput, TradeIdea

log = logging.getLogger(__name__)

ReasonerReason = Literal[
    "reasoner_refusal",
    "reasoner_max_tokens",
    "reasoner_schema",
    "reasoner_timeout",
    "reasoner_infra",
]


class ReasonerFailure(Exception):
    """The call produced no usable output. ``infra`` marks timeouts, connection, 5xx, 429 and auth failures."""

    def __init__(self, reason: ReasonerReason, *, infra: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.infra = infra


class Reasoner(Protocol):
    """Turns a cycle's evidence into intent."""

    def verify(self) -> None:
        """Raise ``ReasonerUnavailable`` if the Reasoner cannot run."""
        ...

    def evaluate(self, input: ReasonerInput) -> ReasonerOutput:
        """Return the model's assessments and ideas. Raises ``ReasonerFailure``."""
        ...


def parse_output(text: str) -> ReasonerOutput:
    """Parse and validate the model's JSON. Raises ``ReasonerFailure("reasoner_schema")``.

    The envelope must match exactly (unknown fields are a schema failure, LP §4.6). An idea that is individually
    invalid, for example a malformed ``size_intent``, is dropped whole and logged (AS §8.1) rather than costing the
    cycle its other ideas.
    """
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ReasonerFailure("reasoner_schema") from e
    if not isinstance(doc, dict):
        raise ReasonerFailure("reasoner_schema")
    ideas = doc.get("ideas")
    if isinstance(ideas, list):
        kept: list[Any] = []
        for position, idea in enumerate(ideas):
            try:
                TradeIdea.model_validate(idea)
            except ValidationError as e:
                log.warning("reasoner idea %d dropped: %d validation errors", position, e.error_count())
            else:
                kept.append(idea)
        doc = {**doc, "ideas": kept}
    try:
        return ReasonerOutput.model_validate(doc)
    except ValidationError as e:
        raise ReasonerFailure("reasoner_schema") from e


class _Stream(Protocol):
    def __enter__(self) -> Self: ...
    def __exit__(self, *exc: object) -> None: ...
    def get_final_message(self) -> Any: ...


class _Messages(Protocol):
    def stream(self, **kwargs: Any) -> _Stream: ...


class _Client(Protocol):
    @property
    def messages(self) -> _Messages: ...


class AnthropicReasoner:
    """Calls ``llm.reasoner_model``: no tools, no web, adaptive thinking, explicit effort, streamed, structured output."""

    def __init__(
        self, prompt: ReasonerPrompt, config: LLMConfig, limits: ProposalConfig, client: _Client | None = None
    ) -> None:
        self._prompt = prompt
        self._model = config.reasoner_model
        self._max_tokens = config.reasoner_max_tokens
        self._effort = config.reasoner_effort
        self._timeout = config.reasoner_timeout_s
        self._system = prompt.render_system(limits)
        self.last_model_id: str | None = None  # the model that actually answered (refusal fallbacks may switch it)
        if prompt.target_model != self._model:
            log.warning(
                "reasoner prompt %s was tested on %s, configured model is %s",
                prompt.version,
                prompt.target_model,
                self._model,
            )
        # The SDK retries 408/409/429/5xx and timeouts itself.
        self._client: _Client = client or cast(
            _Client, anthropic.Anthropic(max_retries=config.reasoner_max_retries, timeout=float(self._timeout))
        )

    def verify(self) -> None:
        """The prompt was verified when loaded; nothing further to check."""

    def evaluate(self, input: ReasonerInput) -> ReasonerOutput:
        """One streamed call. Raises ``ReasonerFailure`` with the LP §4.1 reason code."""
        user = self._prompt.render_user(input)
        try:
            with self._client.messages.stream(
                model=self._model,
                max_tokens=self._max_tokens,
                system=self._system,
                messages=[{"role": "user", "content": user}],
                thinking={"type": "adaptive"},
                output_config={
                    "effort": self._effort,
                    "format": {"type": "json_schema", "schema": self._prompt.schema},
                },
                timeout=float(self._timeout),
            ) as stream:
                resp = stream.get_final_message()
        except anthropic.APITimeoutError as e:
            raise ReasonerFailure("reasoner_timeout", infra=True) from e
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            raise ReasonerFailure("reasoner_infra", infra=True) from e
        except Exception as e:  # missing or unresolvable credentials surface as TypeError / AnthropicError
            raise ReasonerFailure("reasoner_infra", infra=True) from e
        model = getattr(resp, "model", None)
        self.last_model_id = model if isinstance(model, str) else None
        stop = str(resp.stop_reason)
        if stop == "refusal":
            raise ReasonerFailure("reasoner_refusal")
        if stop == "max_tokens":
            raise ReasonerFailure("reasoner_max_tokens")
        text = "".join(b.text for b in resp.content if b.type == "text")
        return parse_output(text)


class UnavailableReasoner:
    """Stands in when the Reasoner cannot be built (for example a prompt integrity failure)."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def verify(self) -> None:
        """Always raises."""
        raise ReasonerUnavailable(self._reason)

    def evaluate(self, input: ReasonerInput) -> ReasonerOutput:
        """Always raises."""
        raise ReasonerUnavailable(self._reason)


def build_reasoner(config: LLMConfig, limits: ProposalConfig, client: _Client | None = None) -> Reasoner:
    """The real Reasoner, or an ``UnavailableReasoner`` if the prompt fails its integrity check."""
    try:
        return AnthropicReasoner(load_reasoner_prompt(), config, limits, client)
    except ReasonerUnavailable as e:
        return UnavailableReasoner(str(e))
