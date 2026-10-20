"""The alert catalogue (AS §9.2): the single place alert text lives.

Every function is a pure constructor: no clock, no I/O. ``alert_id``, ``occurred_at`` and the per-channel delivery
state are filled in by ``notify.outbox.enqueue``. A unit test asserts every ``needs_action`` template carries at
least one literal command (TS AC-04); here that is enforced structurally by deriving ``needs_action`` from whether
``commands`` is non-empty.

Text comes from ``DEFAULTS`` (``str.format`` templates keyed by alert kind). The owner may override a kind's title,
body and severity in ``config.yaml`` under ``ops.notification_catalogue``; ``use_catalogue`` installs those
overrides (the composition root and the tick call it). An override that cannot be formatted -- an unknown
``{placeholder}`` or a stray brace -- is ignored and the default is used, so a typo in the catalogue can never stop
an alert from being raised. Commands, ``fields`` and dedupe keys are structural and are not overridable.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from stockmanager.notify.outbox import Alert, Severity

if TYPE_CHECKING:
    from stockmanager.config.model import NotificationTemplate

#: kind -> (severity, title, body) ``str.format`` templates. The built-in text, used when no override is installed.
DEFAULTS: dict[str, tuple[Severity, str, str]] = {
    "evaluation_complete": ("info", "Evaluation complete: {eval_id}", "Evaluation {eval_id} for {cycle_date} is complete.{late}"),
    "veto_window_open": (
        "info",
        "Veto window open: {proposal_id}",
        "The veto window for {proposal_id} closes in {window_hours}h, at {deadline}.",
    ),
    "trade_executed": (
        "positive",
        "Trade executed: {action} {ticker}",
        "{action} {shares} {ticker} for {value} (mock fill, no real money moved).",
    ),
    "risk_alert": ("alert", "Risk alert: {metric}", "{detail}"),
    "earnings_approaching": ("info", "Earnings approaching: {ticker}", "{ticker} reports earnings on {date}."),
    "system_pause_resume": ("info", "System {action}d", "The system was {action}d."),
    "proposal_expired": ("alert", "Proposal expired: {proposal_id}", "{reason}"),
    "proposal_voided": (
        "alert",
        "Proposal voided: {proposal_id}",
        "{proposal_id} was voided at execution ({reason}). Nothing was filled.",
    ),
    "reconcile_divergence": ("alert", "Reconcile divergence", "The books diverged: {signature}"),
    "rejected_control_message": ("alert", "Rejected control message", "A command from {sender} was rejected."),
    "price_source_failure": ("alert", "Price source failure: {job}", "{job}: {outcome}"),
    "run_skipped_busy": ("alert", "Run skipped: {job}", "{job} skipped slot {slot}: the portfolio was busy."),
    "system_ready": ("info", "System ready", "The system is ready."),
    "config_error": (
        "alert",
        "Config error",
        "config.yaml (sha {bad_sha}) is invalid; running on the last-good mirror.",
    ),
    "news_unavailable": (
        "info",
        "News unavailable",
        "News was unavailable for {day}; no proposals were generated this cycle.",
    ),
    "bot_offline": ("alert", "Bot offline", "The Discord bot heartbeat is stale; commands via CLI meanwhile."),
    "scheduler_stalled": (
        "alert",
        "Scheduler stalled",
        "The scheduler has not ticked recently; a disabled cron may be the cause.",
    ),
    "clock_backwards": (
        "alert",
        "Clock moved backwards",
        "The system clock moved backwards; time-dependent jobs were skipped this tick.",
    ),
    "calendar_coverage": ("info", "Calendar coverage", "The session calendar has no coverage for {day}."),
    "final_report": (
        "info",
        "Final report: portfolio closed",
        ("Closed as of {as_of}: ending value {ending_value}, ROI {roi_pct}%, priced from {source}. Positions were not sold."),
    ),
}

_overrides: dict[str, NotificationTemplate] = {}


def use_catalogue(entries: Iterable[NotificationTemplate]) -> None:
    """Install the owner's ``ops.notification_catalogue`` overrides; an empty iterable restores the defaults."""
    _overrides.clear()
    _overrides.update({e.kind: e for e in entries})


def _format(template: str, variables: dict[str, Any]) -> str:
    return template.format_map(variables)


def render(
    kind: str,
    *,
    fields: dict[str, str] | None = None,
    commands: list[str] | None = None,
    dedupe_key: str | None,
    binds: dict[str, Any] | None = None,
    severity: Severity | None = None,
    **variables: Any,
) -> Alert:
    """Build the ``kind`` alert: the catalogue override if it formats, else the built-in text in ``DEFAULTS``.

    ``variables`` fill the ``{placeholders}`` of the title and body. ``severity`` is the caller's value for the few
    kinds whose severity depends on the situation; otherwise the template's own applies, and an override that sets
    ``severity`` explicitly beats both.
    """
    default_severity, default_title, default_body = DEFAULTS[kind]
    chosen: Severity = severity or default_severity
    title, body = _format(default_title, variables), _format(default_body, variables)
    override = _overrides.get(kind)
    if override is not None:
        try:
            title, body = _format(override.title, variables), _format(override.body, variables)
        except (KeyError, IndexError, ValueError):
            title, body = _format(default_title, variables), _format(default_body, variables)  # unusable override
        else:
            if "severity" in override.model_fields_set:
                chosen = override.severity
    commands = commands or []
    return Alert(
        kind=kind,
        severity=chosen,
        title=title,
        body=body,
        fields=fields or {},
        commands=commands,
        needs_action=bool(commands),
        dedupe_key=dedupe_key,
        binds=binds or {},
    )


def evaluation_complete(eval_id: str, cycle_date: date, ran_late: bool = False) -> Alert:
    """Evaluation finished (AS §9.2 row 1)."""
    return render(
        "evaluation_complete",
        eval_id=eval_id,
        cycle_date=cycle_date.isoformat(),
        late=" (ran late)" if ran_late else "",
        fields={"eval_id": eval_id, "cycle_date": cycle_date.isoformat(), "ran_late": str(ran_late)},
        dedupe_key=f"eval:{cycle_date.isoformat()}",
    )


def veto_window_open(proposal_id: str, window_hours: int) -> Alert:
    """A proposal's veto window opened (AS §9.2 row 2). ``{deadline}`` is late-bound at delivery time (AS §8.3)."""
    return render(
        "veto_window_open",
        proposal_id=proposal_id,
        window_hours=window_hours,
        deadline="{deadline}",
        fields={"proposal_id": proposal_id, "window_hours": str(window_hours)},
        commands=[f"VETO {proposal_id} [reason]", "PAUSE"],
        dedupe_key=f"veto_window_open:{proposal_id}",
        binds={"window_hours": window_hours},
    )


def trade_executed(trade_id: str, action: str, ticker: str, shares: Decimal, value: Decimal) -> Alert:
    """A fill was recorded (AS §9.2 row 3)."""
    return render(
        "trade_executed",
        action=action,
        ticker=ticker,
        shares=shares,
        value=value,
        fields={"trade_id": trade_id, "action": action, "ticker": ticker, "shares": str(shares), "value": str(value)},
        dedupe_key=f"trade_executed:{trade_id}",
    )


def risk_alert(metric: str, cycle_date: date, detail: str) -> Alert:
    """A risk metric breached its threshold (AS §9.2 row 4)."""
    return render(
        "risk_alert",
        metric=metric,
        detail=detail,
        fields={"metric": metric, "cycle_date": cycle_date.isoformat()},
        commands=["PAUSE"],
        dedupe_key=f"risk:{metric}:{cycle_date.isoformat()}",
    )


def earnings_approaching(ticker: str, date_: date) -> Alert:
    """A holding's earnings date is within 7 days (AS §9.2 row 5)."""
    return render(
        "earnings_approaching",
        ticker=ticker,
        date=date_.isoformat(),
        fields={"ticker": ticker, "date": date_.isoformat()},
        dedupe_key=f"earnings:{ticker}:{date_.isoformat()}",
    )


def system_pause_resume(action: str) -> Alert:
    """The system was paused or resumed (AS §9.2 row 6). ``action`` is ``"pause"`` or ``"resume"``."""
    return render(
        "system_pause_resume",
        action=action,
        fields={"action": action},
        commands=["RESUME"] if action == "pause" else [],
        dedupe_key=f"system_pause_resume:{action}",
    )


def proposal_expired(proposal_id: str, reason: str) -> Alert:
    """A proposal expired (AS §9.2 row 7)."""
    return render(
        "proposal_expired",
        proposal_id=proposal_id,
        reason=reason,
        fields={"proposal_id": proposal_id},
        commands=[f"RECONFIRM {proposal_id}"],
        dedupe_key=f"expired:{proposal_id}",
    )


def proposal_voided(proposal_id: str, reason: str) -> Alert:
    """Execution voided a proposal (AS §9.2 row 7, "also voided"; TS NN-D12).

    Unlike ``proposal_expired`` it offers no ``RECONFIRM``: that command only accepts an ``EXPIRED`` proposal.
    """
    return render(
        "proposal_voided",
        proposal_id=proposal_id,
        reason=reason,
        fields={"proposal_id": proposal_id, "reason": reason},
        commands=["STATUS"],
        dedupe_key=f"voided:{proposal_id}",
    )


def reconcile_divergence(signature: str) -> Alert:
    """The books disagree with themselves (AS §9.2 row 8)."""
    return render(
        "reconcile_divergence",
        signature=signature,
        fields={"signature": signature},
        commands=["RECONCILE rebuild", "STATUS"],
        dedupe_key=f"recon:{signature}",
    )


def rejected_control_message(sender: str) -> Alert:
    """An unauthorized or malformed command was rejected (AS §9.2 row 9); rate-limited by the caller."""
    return render(
        "rejected_control_message",
        sender=sender,
        fields={"sender": sender},
        dedupe_key=f"rejected:{sender}",
    )


def price_source_failure(job: str, tickers: list[str], fallback_worked: bool) -> Alert:
    """A price provider failed (AS §9.2 row 10); severity depends on whether the fallback covered it."""
    tickers_str = ",".join(tickers)
    outcome = (
        f"primary price source failed for {tickers_str}; fallback succeeded."
        if fallback_worked
        else f"both price sources failed for {tickers_str}."
    )
    return render(
        "price_source_failure",
        severity="info" if fallback_worked else "alert",
        job=job,
        outcome=outcome,
        fields={"job": job, "tickers": tickers_str, "fallback_worked": str(fallback_worked)},
        commands=["STATUS"],
        dedupe_key=f"price:{job}:{tickers_str}",
    )


def run_skipped_busy(job: str, slot: str) -> Alert:
    """A scheduled job was skipped because the lock was busy (AS §9.2 row 11)."""
    return render(
        "run_skipped_busy",
        job=job,
        slot=slot,
        fields={"job": job, "slot": slot},
        commands=["STATUS"],
        dedupe_key=f"busy:{job}:{slot}",
    )


def system_ready() -> Alert:
    """First successful run, or ``doctor`` passing (AS §9.2 row 12)."""
    return render("system_ready", dedupe_key="system_ready")


def config_error(bad_sha: str) -> Alert:
    """``config.yaml`` is invalid; running on the last-good mirror (AS §9.2 row 13)."""
    return render("config_error", bad_sha=bad_sha, fields={"bad_sha": bad_sha}, dedupe_key=f"config:{bad_sha}")


def news_unavailable(day: date) -> Alert:
    """News fetch failed entirely for ``day`` (AS §9.2 row 14)."""
    return render(
        "news_unavailable",
        day=day.isoformat(),
        fields={"day": day.isoformat()},
        commands=["STATUS"],
        dedupe_key=f"news_unavailable:{day.isoformat()}",
    )


def bot_offline() -> Alert:
    """The Discord bot heartbeat is stale (AS §9.2 row 14)."""
    return render("bot_offline", commands=["STATUS"], dedupe_key="bot_offline")


def scheduler_stalled() -> Alert:
    """The scheduler heartbeat is stale (AS §9.2 row 14)."""
    return render("scheduler_stalled", commands=["STATUS"], dedupe_key="scheduler_stalled")


def clock_backwards() -> Alert:
    """The system clock moved backwards (AS §9.2 row 14)."""
    return render("clock_backwards", commands=["STATUS"], dedupe_key="clock_backwards")


def calendar_coverage(day: date) -> Alert:
    """The session calendar lacks coverage for ``day`` (AS §9.2 row 14)."""
    return render(
        "calendar_coverage",
        day=day.isoformat(),
        fields={"day": day.isoformat()},
        commands=["STATUS"],
        dedupe_key=f"calendar_coverage:{day.isoformat()}",
    )


def final_report(ending_value: str, roi_pct: str | None, source: str, as_of: date) -> Alert:
    """``CLOSE CONFIRM`` recorded the final valuation (UX §8, TS CMD-C02)."""
    return render(
        "final_report",
        ending_value=ending_value,
        roi_pct=roi_pct if roi_pct is not None else "n/a",
        source=source,
        as_of=as_of.isoformat(),
        fields={"ending_value": ending_value, "roi_pct": roi_pct or "", "source": source, "as_of": as_of.isoformat()},
        dedupe_key="final_report",
    )
