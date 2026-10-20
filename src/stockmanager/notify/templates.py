"""The alert catalogue (AS §9.2): the single place alert text lives.

Every function is a pure constructor: no clock, no I/O. ``alert_id``, ``occurred_at`` and the per-channel delivery
state are filled in by ``notify.outbox.enqueue``. A unit test asserts every ``needs_action`` template carries at
least one literal command (TS AC-04); here that is enforced structurally by deriving ``needs_action`` from whether
``commands`` is non-empty.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from stockmanager.notify.outbox import Alert, Severity


def _alert(
    kind: str,
    severity: Severity,
    title: str,
    body: str,
    *,
    fields: dict[str, str] | None = None,
    commands: list[str] | None = None,
    dedupe_key: str | None,
    binds: dict[str, Any] | None = None,
) -> Alert:
    commands = commands or []
    return Alert(
        kind=kind,
        severity=severity,
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
    body = f"Evaluation {eval_id} for {cycle_date.isoformat()} is complete."
    if ran_late:
        body += " (ran late)"
    return _alert(
        "evaluation_complete",
        "info",
        f"Evaluation complete: {eval_id}",
        body,
        fields={"eval_id": eval_id, "cycle_date": cycle_date.isoformat(), "ran_late": str(ran_late)},
        dedupe_key=f"eval:{cycle_date.isoformat()}",
    )


def veto_window_open(proposal_id: str, window_hours: int) -> Alert:
    """A proposal's veto window opened (AS §9.2 row 2). ``{deadline}`` is late-bound at render time (AS §8.3)."""
    return _alert(
        "veto_window_open",
        "info",
        f"Veto window open: {proposal_id}",
        f"The veto window for {proposal_id} closes in {window_hours}h, at {{deadline}}.",
        fields={"proposal_id": proposal_id, "window_hours": str(window_hours)},
        commands=[f"VETO {proposal_id} [reason]", "PAUSE"],
        dedupe_key=f"veto_window_open:{proposal_id}",
        binds={"window_hours": window_hours},
    )


def trade_executed(trade_id: str, action: str, ticker: str, shares: Decimal, value: Decimal) -> Alert:
    """A fill was recorded (AS §9.2 row 3)."""
    return _alert(
        "trade_executed",
        "positive",
        f"Trade executed: {action} {ticker}",
        f"{action} {shares} {ticker} for {value} (mock fill, no real money moved).",
        fields={"trade_id": trade_id, "action": action, "ticker": ticker, "shares": str(shares), "value": str(value)},
        dedupe_key=f"trade_executed:{trade_id}",
    )


def risk_alert(metric: str, cycle_date: date, detail: str) -> Alert:
    """A risk metric breached its threshold (AS §9.2 row 4)."""
    return _alert(
        "risk_alert",
        "alert",
        f"Risk alert: {metric}",
        detail,
        fields={"metric": metric, "cycle_date": cycle_date.isoformat()},
        commands=["PAUSE"],
        dedupe_key=f"risk:{metric}:{cycle_date.isoformat()}",
    )


def earnings_approaching(ticker: str, date_: date) -> Alert:
    """A holding's earnings date is within 7 days (AS §9.2 row 5)."""
    return _alert(
        "earnings_approaching",
        "info",
        f"Earnings approaching: {ticker}",
        f"{ticker} reports earnings on {date_.isoformat()}.",
        fields={"ticker": ticker, "date": date_.isoformat()},
        dedupe_key=f"earnings:{ticker}:{date_.isoformat()}",
    )


def system_pause_resume(action: str) -> Alert:
    """The system was paused or resumed (AS §9.2 row 6). ``action`` is ``"pause"`` or ``"resume"``."""
    commands = ["RESUME"] if action == "pause" else []
    return _alert(
        "system_pause_resume",
        "info",
        f"System {action}d",
        f"The system was {action}d.",
        fields={"action": action},
        commands=commands,
        dedupe_key=f"system_pause_resume:{action}",
    )


def proposal_expired(proposal_id: str, reason: str) -> Alert:
    """A proposal expired or was voided (AS §9.2 row 7)."""
    return _alert(
        "proposal_expired",
        "alert",
        f"Proposal expired: {proposal_id}",
        reason,
        fields={"proposal_id": proposal_id},
        commands=[f"RECONFIRM {proposal_id}"],
        dedupe_key=f"expired:{proposal_id}",
    )


def reconcile_divergence(signature: str) -> Alert:
    """The books disagree with themselves (AS §9.2 row 8)."""
    return _alert(
        "reconcile_divergence",
        "alert",
        "Reconcile divergence",
        f"The books diverged: {signature}",
        fields={"signature": signature},
        commands=["RECONCILE rebuild", "STATUS"],
        dedupe_key=f"recon:{signature}",
    )


def rejected_control_message(sender: str) -> Alert:
    """An unauthorized or malformed command was rejected (AS §9.2 row 9); rate-limited by the caller."""
    return _alert(
        "rejected_control_message",
        "alert",
        "Rejected control message",
        f"A command from {sender} was rejected.",
        fields={"sender": sender},
        dedupe_key=f"rejected:{sender}",
    )


def price_source_failure(job: str, tickers: list[str], fallback_worked: bool) -> Alert:
    """A price provider failed (AS §9.2 row 10); severity depends on whether the fallback covered it."""
    severity: Severity = "info" if fallback_worked else "alert"
    tickers_str = ",".join(tickers)
    body = (
        f"{job}: primary price source failed for {tickers_str}; fallback succeeded."
        if fallback_worked
        else f"{job}: both price sources failed for {tickers_str}."
    )
    return _alert(
        "price_source_failure",
        severity,
        f"Price source failure: {job}",
        body,
        fields={"job": job, "tickers": tickers_str, "fallback_worked": str(fallback_worked)},
        commands=["STATUS"],
        dedupe_key=f"price:{job}:{tickers_str}",
    )


def run_skipped_busy(job: str, slot: str) -> Alert:
    """A scheduled job was skipped because the lock was busy (AS §9.2 row 11)."""
    return _alert(
        "run_skipped_busy",
        "alert",
        f"Run skipped: {job}",
        f"{job} skipped slot {slot}: the portfolio was busy.",
        fields={"job": job, "slot": slot},
        commands=["STATUS"],
        dedupe_key=f"busy:{job}:{slot}",
    )


def system_ready() -> Alert:
    """First successful run, or ``doctor`` passing (AS §9.2 row 12)."""
    return _alert("system_ready", "info", "System ready", "The system is ready.", dedupe_key="system_ready")


def config_error(bad_sha: str) -> Alert:
    """``config.yaml`` is invalid; running on the last-good mirror (AS §9.2 row 13)."""
    return _alert(
        "config_error",
        "alert",
        "Config error",
        f"config.yaml (sha {bad_sha}) is invalid; running on the last-good mirror.",
        fields={"bad_sha": bad_sha},
        dedupe_key=f"config:{bad_sha}",
    )


def news_unavailable(day: date) -> Alert:
    """News fetch failed entirely for ``day`` (AS §9.2 row 14)."""
    return _alert(
        "news_unavailable",
        "info",
        "News unavailable",
        f"News was unavailable for {day.isoformat()}; no proposals were generated this cycle.",
        fields={"day": day.isoformat()},
        commands=["STATUS"],
        dedupe_key=f"news_unavailable:{day.isoformat()}",
    )


def bot_offline() -> Alert:
    """The Discord bot heartbeat is stale (AS §9.2 row 14)."""
    return _alert(
        "bot_offline",
        "alert",
        "Bot offline",
        "The Discord bot heartbeat is stale; commands via CLI meanwhile.",
        commands=["STATUS"],
        dedupe_key="bot_offline",
    )


def scheduler_stalled() -> Alert:
    """The scheduler heartbeat is stale (AS §9.2 row 14)."""
    return _alert(
        "scheduler_stalled",
        "alert",
        "Scheduler stalled",
        "The scheduler has not ticked recently; a disabled cron may be the cause.",
        commands=["STATUS"],
        dedupe_key="scheduler_stalled",
    )


def clock_backwards() -> Alert:
    """The system clock moved backwards (AS §9.2 row 14)."""
    return _alert(
        "clock_backwards",
        "alert",
        "Clock moved backwards",
        "The system clock moved backwards; time-dependent jobs were skipped this tick.",
        commands=["STATUS"],
        dedupe_key="clock_backwards",
    )


def calendar_coverage(day: date) -> Alert:
    """The session calendar lacks coverage for ``day`` (AS §9.2 row 14)."""
    return _alert(
        "calendar_coverage",
        "info",
        "Calendar coverage",
        f"The session calendar has no coverage for {day.isoformat()}.",
        fields={"day": day.isoformat()},
        commands=["STATUS"],
        dedupe_key=f"calendar_coverage:{day.isoformat()}",
    )
