"""``ops.visible_channels`` (AS §3.6) and ``ops.notification_catalogue`` (SS §10)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stockmanager.config.model import BLOCKED_SET_PREFIXES, SETTABLE_KEYS, Config, NotificationTemplate, OpsConfig


def test_defaults() -> None:
    ops = Config().ops
    assert ops.visible_channels == ["discord"]
    assert ops.notification_catalogue == []


def test_defaults_not_shared() -> None:
    a, b = OpsConfig(), OpsConfig()
    a.visible_channels.append("other")
    assert b.visible_channels == ["discord"]


def test_visible_channels_override_and_empty() -> None:
    assert OpsConfig(visible_channels=["discord", "chat"]).visible_channels == ["discord", "chat"]
    assert OpsConfig(visible_channels=[]).visible_channels == []


@pytest.mark.parametrize("bad", [["discord", "discord"], [" "], [""]])
def test_visible_channels_rejects_invalid(bad: list[str]) -> None:
    with pytest.raises(ValidationError):
        OpsConfig(visible_channels=bad)


def test_catalogue_parses_templates() -> None:
    ops = OpsConfig.model_validate(
        {"notification_catalogue": [{"kind": "trade_executed", "severity": "positive", "title": "Trade executed"}]}
    )
    assert ops.notification_catalogue == [
        NotificationTemplate(kind="trade_executed", severity="positive", title="Trade executed")
    ]


def test_catalogue_rejects_duplicates_and_extras() -> None:
    t = {"kind": "x", "title": "X"}
    with pytest.raises(ValidationError):
        OpsConfig.model_validate({"notification_catalogue": [t, t]})
    with pytest.raises(ValidationError):
        OpsConfig.model_validate({"notification_catalogue": [{**t, "bogus": 1}]})
    with pytest.raises(ValidationError):
        OpsConfig.model_validate({"notification_catalogue": [{**t, "severity": "loud"}]})


def test_not_settable() -> None:
    assert "ops." in BLOCKED_SET_PREFIXES
    assert not any(k.endswith(("visible_channels", "notification_catalogue")) for k in SETTABLE_KEYS)
