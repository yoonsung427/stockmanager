"""``ops.notification_catalogue`` overrides (SS §10, AS §9.2): defaults, overrides, and bad overrides."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest

from stockmanager.config.model import Config, NotificationTemplate
from stockmanager.notify import templates
from stockmanager.notify.templates import (
    DEFAULTS,
    evaluation_complete,
    final_report,
    price_source_failure,
    proposal_voided,
    risk_alert,
    trade_executed,
    use_catalogue,
    veto_window_open,
)


@pytest.fixture(autouse=True)
def clean_catalogue() -> Iterator[None]:
    use_catalogue([])
    yield
    use_catalogue([])


def test_defaults_keep_the_original_text() -> None:
    alert = evaluation_complete("eval-2026-10-24", date(2026, 10, 24), ran_late=True)
    assert alert.title == "Evaluation complete: eval-2026-10-24"
    assert alert.body == "Evaluation eval-2026-10-24 for 2026-10-24 is complete. (ran late)"
    assert evaluation_complete("e", date(2026, 10, 24)).body == "Evaluation e for 2026-10-24 is complete."


def test_veto_window_keeps_its_late_bound_deadline_placeholder() -> None:
    assert veto_window_open("P-2026-10-24-01", 4).body.endswith("closes in 4h, at {deadline}.")


def test_every_default_template_formats_and_has_a_matching_constructor() -> None:
    assert {"final_report", "proposal_voided", "reconcile_divergence", "run_skipped_busy"} <= set(DEFAULTS)
    assert price_source_failure("j", ["A", "B"], fallback_worked=True).severity == "info"
    assert price_source_failure("j", ["A", "B"], fallback_worked=False).severity == "alert"


def test_an_override_replaces_title_and_body_but_not_commands_or_fields() -> None:
    use_catalogue(
        [NotificationTemplate(kind="trade_executed", title="Filled {ticker}", body="{action} {shares} {ticker} @ {value}")]
    )
    alert = trade_executed("P-1-buy1", "BUY", "AAPL", Decimal("5.0000"), Decimal("1000.0000"))
    assert (alert.title, alert.body) == ("Filled AAPL", "BUY 5.0000 AAPL @ 1000.0000")
    assert alert.severity == "positive"  # the entry did not set a severity, so the template's own stands
    assert alert.fields["trade_id"] == "P-1-buy1" and alert.dedupe_key == "trade_executed:P-1-buy1"


def test_an_explicit_severity_in_the_override_wins() -> None:
    use_catalogue([NotificationTemplate(kind="trade_executed", severity="info", title="t", body="b")])
    assert trade_executed("x", "BUY", "A", Decimal(1), Decimal(1)).severity == "info"


def test_needs_action_cannot_be_overridden_away() -> None:
    use_catalogue([NotificationTemplate(kind="risk_alert", title="r", body="b")])
    alert = risk_alert("drawdown", date(2026, 10, 24), "x")
    assert alert.commands == ["PAUSE"] and alert.needs_action


@pytest.mark.parametrize("body", ["{nope}", "{unclosed", "{0}", "{ticker.__class__}x{"])
def test_an_unusable_override_falls_back_to_the_default(body: str) -> None:
    use_catalogue([NotificationTemplate(kind="trade_executed", title="ok {ticker}", body=body)])
    alert = trade_executed("x", "BUY", "AAPL", Decimal(1), Decimal(2))
    assert alert.title == "Trade executed: BUY AAPL"
    assert alert.body.startswith("BUY 1 AAPL for 2")


def test_use_catalogue_replaces_rather_than_merges() -> None:
    use_catalogue([NotificationTemplate(kind="system_ready", title="A", body="b")])
    use_catalogue([])
    assert templates.system_ready().title == "System ready"


def test_config_rejects_duplicate_catalogue_kinds() -> None:
    entry = {"kind": "system_ready", "title": "x"}
    with pytest.raises(ValueError, match="unique"):
        Config.model_validate({"ops": {"notification_catalogue": [entry, entry]}})


def test_new_templates() -> None:
    voided = proposal_voided("P-2026-10-24-01", "cap_breach_at_execution")
    assert voided.commands == ["STATUS"] and "cap_breach_at_execution" in voided.body
    report = final_report("52000.0000", None, "yahoo", date(2026, 10, 23))
    assert "ROI n/a%" in report.body and report.dedupe_key == "final_report"
