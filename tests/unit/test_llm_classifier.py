"""LLM classifier adapter (v2 R4, R14): strict JSON, reasoning_content primary, fail closed. No live HTTP."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from stockmanager.config.triage import TriageConfig
from stockmanager.triage.classifier import ClassifierUnavailable
from stockmanager.triage.llm_classifier import LLMClassifier
from stockmanager.triage.models import Classification, EventKind, TriageItem

ITEM = TriageItem(
    "h1", "https://news.example/a", "AAPL beats earnings", "Apple reported.", datetime(2026, 10, 25, tzinfo=UTC)
)
ANSWER = json.dumps({"event_kind": "earnings", "confidence": 0.92})


def _reply(message: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"choices": [{"message": message}]})


def _clf(handler: Any, key: str = "k") -> LLMClassifier:
    return LLMClassifier("http://llm.test/v1/", "m", key, transport=httpx.MockTransport(handler))


def _classify(handler: Any) -> Classification:
    return _clf(handler).classify(ITEM, "AAPL", [EventKind.EARNINGS])


def test_strict_json_in_content() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return _reply({"content": ANSWER})

    assert _classify(handler) == Classification(EventKind.EARNINGS, 0.92)
    req = seen[0]
    assert str(req.url) == "http://llm.test/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer k"
    body = json.loads(req.content)
    assert body["model"] == "m" and body["max_tokens"] == 2048


def test_reasoning_content_only_is_parsed() -> None:  # R14: probe shape, content null
    assert _classify(lambda r: _reply({"content": None, "reasoning_content": ANSWER})) == Classification(
        EventKind.EARNINGS, 0.92
    )


def test_reasoning_content_is_primary_over_content() -> None:
    other = json.dumps({"event_kind": "legal", "confidence": 0.5})
    assert _classify(lambda r: _reply({"content": other, "reasoning_content": ANSWER})).event_kind is EventKind.EARNINGS


def test_content_is_fallback_when_reasoning_is_prose() -> None:
    assert _classify(lambda r: _reply({"content": ANSWER, "reasoning_content": "Let me think..."})).confidence == 0.92


def test_event_id_is_carried() -> None:
    text = json.dumps({"event_kind": "legal", "confidence": 1, "event_id": "e-1"})
    assert _classify(lambda r: _reply({"content": text})) == Classification(EventKind.LEGAL, 1.0, "e-1")


def test_null_content_and_null_reasoning_fails_closed() -> None:
    with pytest.raises(ClassifierUnavailable):
        _classify(lambda r: _reply({"content": None, "reasoning_content": None}))


def test_timeout_fails_closed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(ClassifierUnavailable, match="timed out"):
        _classify(handler)


def test_transport_error_fails_closed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=req)

    with pytest.raises(ClassifierUnavailable):
        _classify(handler)


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_error_status_fails_closed(status: int) -> None:
    with pytest.raises(ClassifierUnavailable):
        _classify(lambda r: _reply({"content": ANSWER}, status))


@pytest.mark.parametrize(
    "text",
    [
        '{"event_kind": "sports", "confidence": 0.9}',  # out of enum
        '{"event_kind": "earnings", "confidence": 1.5}',
        '{"event_kind": "earnings", "confidence": -0.1}',
        '{"event_kind": "earnings", "confidence": "0.9"}',
        '{"event_kind": "earnings", "confidence": true}',
        '{"event_kind": "earnings"}',
        '{"event_kind": "earnings", "confidence": 0.9, "extra": 1}',
        '{"event_kind": "earnings", "confidence": 0.9} trailing',
        "not json",
        "[]",
        "",
    ],
)
def test_contract_violations_fail_closed(text: str) -> None:
    with pytest.raises(ClassifierUnavailable):
        _classify(lambda r: _reply({"content": text}))


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": "x"}]}, [1]])
def test_malformed_envelope_fails_closed(payload: Any) -> None:
    with pytest.raises(ClassifierUnavailable):
        _classify(lambda r: httpx.Response(200, json=payload))


def test_non_json_body_fails_closed() -> None:
    with pytest.raises(ClassifierUnavailable):
        _classify(lambda r: httpx.Response(200, text="<html>"))


def test_refusal_fails_closed_even_with_valid_json() -> None:
    with pytest.raises(ClassifierUnavailable, match="refused"):
        _classify(lambda r: _reply({"content": ANSWER, "refusal": "I can't help with that."}))


def test_unconfigured_endpoint_makes_no_request() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    clf = LLMClassifier.from_config(TriageConfig(), {}, httpx.MockTransport(handler))
    with pytest.raises(ClassifierUnavailable, match="not configured"):
        clf.classify(ITEM, "AAPL", [])


def test_from_config_reads_key_from_named_env_var() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("authorization", ""))
        return _reply({"content": ANSWER})

    cfg = TriageConfig(classifier_base_url="http://llm.test/v1", classifier_model="m", classifier_api_key_env="MY_KEY")
    LLMClassifier.from_config(cfg, {"MY_KEY": "s3cret"}, httpx.MockTransport(handler)).classify(ITEM, "AAPL", [])
    assert seen == ["Bearer s3cret"]


def test_item_text_is_sent_as_quoted_data() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return _reply({"content": ANSWER})

    _classify(handler)
    user = bodies[0]["messages"][1]["content"]
    assert "<item>" in user and "AAPL beats earnings" in user and "Ticker: AAPL" in user
