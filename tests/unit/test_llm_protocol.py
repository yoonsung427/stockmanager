"""``ChatResult``, stop-reason normalisation and error translation of the LLM backends."""

from __future__ import annotations

import dataclasses
import importlib
from typing import Any

import anthropic
import httpx2
import pytest

from stockmanager.config.llm import LLMConfig
from stockmanager.llm import ChatResult, LLMAuthError, LLMBackend, LLMError, LLMTimeout
from stockmanager.llm.anthropic import AnthropicLLMBackend
from stockmanager.llm.openai_compat import UNAVAILABLE_MODEL_ID, OpenAILLMBackend

REQ = httpx2.Request("POST", "https://example.invalid")
CALL: dict[str, Any] = {"model": "m", "system": "s", "user": "u", "max_tokens": 10, "timeout": 5.0}


def test_chat_result_is_frozen() -> None:
    r = ChatResult(text="t", stop_reason="stop", model_id="m")
    assert (r.text, r.stop_reason, r.model_id) == ("t", "stop", "m")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.text = "x"  # type: ignore[misc]


# -- Anthropic ----------------------------------------------------------------------------------------------------


class Block:
    def __init__(self, text: str, type: str = "text") -> None:
        self.text, self.type = text, type


class Msg:
    def __init__(self, stop: str, text: str = "hi", model: str | None = "claude-x") -> None:
        self.content = [Block("thinking", "thinking"), Block(text)]
        self.stop_reason = stop
        self.model = model


class Client:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def anth(outcome: Any, **kw: Any) -> tuple[AnthropicLLMBackend, Client]:
    client = Client(outcome)
    return AnthropicLLMBackend(LLMConfig(), stream=False, client=client, **kw), client


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [
        ("refusal", "refusal"),
        ("max_tokens", "max_tokens"),
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("tool_use", "tool_calls"),
        ("pause_turn", "stop"),
    ],
)
def test_anthropic_stop_reasons(raw: str, normalised: str) -> None:
    backend, _ = anth(Msg(raw))
    assert backend.chat_completion(**CALL) == ChatResult("hi", normalised, "claude-x")


def test_anthropic_request_keeps_thinking_effort_and_schema() -> None:
    backend, client = anth(Msg("end_turn"), thinking="disabled", effort="low")
    backend.chat_completion(**CALL, json_schema={"type": "object"})
    kw = client.calls[0]
    assert kw["thinking"] == {"type": "disabled"}
    assert kw["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert kw["messages"] == [{"role": "user", "content": "u"}] and kw["system"] == "s" and kw["timeout"] == 5.0


def test_anthropic_defaults_follow_the_reasoner_config() -> None:
    backend, client = anth(Msg("end_turn", model=None))
    result = backend.chat_completion(**CALL)
    assert client.calls[0]["thinking"] == {"type": "adaptive"}
    assert client.calls[0]["output_config"] == {"effort": "high"}
    assert result.model_id == "m"  # falls back to the requested model


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (anthropic.APITimeoutError(request=REQ), LLMTimeout),
        (anthropic.APIConnectionError(request=REQ), LLMError),
        (anthropic.AuthenticationError("x", response=httpx2.Response(401, request=REQ), body=None), LLMAuthError),
        (anthropic.InternalServerError("x", response=httpx2.Response(500, request=REQ), body=None), LLMError),
        (TypeError("Could not resolve authentication method"), LLMAuthError),
    ],
)
def test_anthropic_errors_are_translated(exc: Exception, kind: type[LLMError]) -> None:
    backend, _ = anth(exc)
    with pytest.raises(LLMError) as e:
        backend.chat_completion(**CALL)
    assert type(e.value) is kind


# -- OpenAI-compatible --------------------------------------------------------------------------------------------


class Choice:
    def __init__(self, finish: str, content: str | None = "{}", refusal: str | None = None) -> None:
        self.finish_reason = finish
        self.message = type("M", (), {"content": content, "refusal": refusal})()


class Completions:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return type("R", (), {"choices": [self.outcome], "model": "llama"})()


def oai(outcome: Any) -> tuple[OpenAILLMBackend, Completions]:
    completions = Completions(outcome)
    client = type("C", (), {"chat": type("Chat", (), {"completions": completions})()})()
    return OpenAILLMBackend("http://localhost:11434/v1", "dummy", client=client), completions


@pytest.mark.parametrize(
    ("finish", "normalised"),
    [("stop", "stop"), ("length", "max_tokens"), ("content_filter", "refusal"), ("tool_calls", "tool_calls")],
)
def test_openai_finish_reasons(finish: str, normalised: str) -> None:
    backend, _ = oai(Choice(finish))
    assert backend.chat_completion(**CALL) == ChatResult("{}", normalised, "llama")


def test_openai_refusal_field_wins() -> None:
    backend, _ = oai(Choice("stop", content=None, refusal="no"))
    assert backend.chat_completion(**CALL) == ChatResult("", "refusal", "llama")


def test_openai_uses_json_mode_and_no_anthropic_params() -> None:
    backend, completions = oai(Choice("stop"))
    backend.chat_completion(**CALL, json_schema={"type": "object"})
    kw = completions.calls[0]
    assert kw["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in kw["messages"]] == ["system", "user"]
    assert not {"thinking", "output_config", "effort"} & set(kw)


def test_openai_unexpected_error_is_translated() -> None:
    backend, _ = oai(RuntimeError("boom"))
    with pytest.raises(LLMError):
        backend.chat_completion(**CALL)


def test_openai_missing_package_degrades(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:

    def missing(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", missing)
    with caplog.at_level("WARNING"):
        backend = OpenAILLMBackend("http://localhost:11434/v1")
    assert "not installed" in caplog.text
    assert backend.chat_completion(**CALL) == ChatResult("", "refusal", UNAVAILABLE_MODEL_ID)


def test_backends_satisfy_the_protocol() -> None:
    a: LLMBackend = AnthropicLLMBackend(LLMConfig(), client=Client(None))
    o: LLMBackend = OpenAILLMBackend("http://x", client=object())
    assert a and o
