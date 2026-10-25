"""M5 guardrail audit (v2 requirements M5 / C1, test plan section 3): the recurring report that proves no fill is
traceable only to an LLM-mediated action. ``assert_c1`` is the CI gate: it fails on any violation."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from stockmanager.cli import main
from stockmanager.commands import CommandContext, Sender
from stockmanager.commands.bridge import Bridge
from stockmanager.commands.confirm import SigningKey, bridge_dir
from stockmanager.config import Config, DiscordConfig
from stockmanager.domain.docs import ProposalDoc, Trade
from stockmanager.notify import Receipt, Rendered, deliver_pending
from stockmanager.reporting.guardrail import (
    PATH_ASSISTANT,
    PATH_OPENING,
    PATH_TIMER,
    PATH_VERB,
    AuditEvidence,
    AuditReport,
    BridgeEvidence,
    audit,
    audit_workspace,
    load_evidence,
    render_report,
    report_json,
)
from tests.support.books import PT, make_trade
from tests.support.proposals import advance, make_proposal, with_receipt
from tests.support.world import World

OWNER = "1"
KEY = SigningKey(b"k" * 32)
CREATED = datetime(2026, 10, 12, 7, 0, tzinfo=PT)
DELIVERED = CREATED + timedelta(minutes=5)
EXECUTE_AT = CREATED + timedelta(hours=5)  # after the 4h window that opens at DELIVERED
REF = "0123456789abcdef"


def assert_c1(report: AuditReport) -> None:
    """The CI gate: C1 holds only with zero violations."""
    assert report.passed, "C1 violated:\n" + render_report(report)


# -- fixture builders -----------------------------------------------------------------------------------------------


def trade_for(pid: str) -> Trade:
    return make_trade(1).model_copy(update={"trade_id": f"{pid}-buy1", "timestamp": EXECUTE_AT})


def proposal(
    pid: str,
    approver: str,
    cause: str = "approved",
    *,
    mode: str = "explicit",
    receipt: bool = True,
    window_opened_at: datetime = DELIVERED,
    approved_at: datetime | None = None,
    executor: str = "system:execution",
) -> ProposalDoc:
    """An EXECUTED proposal: PROPOSED -> VETO_WINDOW (sweeper) -> APPROVED (``approver``) -> EXECUTED."""
    doc = make_proposal(pid, state="PROPOSED", created_at=CREATED, execute_at=EXECUTE_AT, approval_mode=mode)
    if receipt:
        doc = with_receipt(doc, DELIVERED)
        doc = doc.model_copy(update={"veto_deadline": doc.delivery.receipts[0].veto_deadline})
    deadline = doc.veto_deadline or DELIVERED + timedelta(hours=4)
    doc = advance(doc, "VETO_WINDOW", window_opened_at, "system:sweeper", "delivered")
    doc = advance(doc, "APPROVED", approved_at or (deadline if approver == "system:sweeper" else DELIVERED + timedelta(minutes=30)), approver, cause)
    return advance(doc, "EXECUTED", EXECUTE_AT, executor, "filled")


def bridge_ev(**overrides: object) -> BridgeEvidence:
    base = BridgeEvidence(
        ref=REF,
        uid=OWNER,
        command="APPROVE P-2026-10-12-05",
        state="dispatched",
        confirmed_by=f"discord:{OWNER}",
        final_command="APPROVE P-2026-10-12-05",
        token_spent=True,
        echo_delivered=True,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


ASSISTANT_CAUSE = f"approved [assistant-confirmed ref={REF} by=discord:{OWNER}]"


def evidence(*docs: ProposalDoc, bridge: dict[str, BridgeEvidence] | None = None, extra: list[Trade] | None = None) -> AuditEvidence:
    trades = [trade_for(d.proposal_id) for d in docs] + (extra or [])
    return AuditEvidence(trades, list(docs), bridge or {})


def traceable_set() -> AuditEvidence:
    """One fill per legitimate trace path."""
    return evidence(
        proposal("P-2026-10-12-01", "cli"),
        proposal("P-2026-10-12-02", f"discord:{OWNER}"),
        proposal("P-2026-10-12-03", "system:sweeper", "auto-approved at deadline", mode="auto"),
        proposal("P-2026-10-12-05", f"assistant:{OWNER}", ASSISTANT_CAUSE),
        bridge={REF: bridge_ev()},
        extra=[make_trade(1)],  # P-OPEN-2026-10-12-buy1
    )


# -- the CI gate ----------------------------------------------------------------------------------------------------


def test_every_fill_traceable_passes() -> None:
    report = audit(traceable_set())
    assert_c1(report)
    assert report.fills == 5 and report.violations == ()
    assert len(report.cycles) == 1
    cycle = report.cycles[0]
    assert dict(cycle.by_path) == {PATH_VERB: 2, PATH_TIMER: 1, PATH_ASSISTANT: 1, PATH_OPENING: 1}
    assert cycle.llm_mediated == 1 and cycle.violations == 0


def test_llm_mediated_actions_are_listed_apart_as_audit_entries() -> None:
    report = audit(traceable_set())
    assert len(report.llm_entries) == 1
    entry = report.llm_entries[0]
    assert entry.actor == f"assistant:{OWNER}"  # never discord:<owner>
    assert entry.confirmed_by == f"discord:{OWNER}" and entry.ref == REF and entry.valid
    assert entry.command == "APPROVE P-2026-10-12-05"


def test_no_fills_passes_with_an_empty_report() -> None:
    report = audit(AuditEvidence([], [], {}))
    assert_c1(report)
    assert report.cycles == () and "(no fills)" in render_report(report)


def _llm_executed(ev: AuditEvidence) -> AuditEvidence:
    """The same fixture set plus one fill whose approval was executed by an LLM-driven actor."""
    bad = proposal("P-2026-10-12-09", "llm:reasoner")
    return AuditEvidence([*ev.trades, trade_for(bad.proposal_id)], [*ev.proposals, bad], ev.bridge)


def test_gate_fails_on_an_llm_executed_irreversible_action() -> None:
    report = audit(_llm_executed(traceable_set()))
    assert not report.passed
    with pytest.raises(AssertionError, match="C1 violated"):
        assert_c1(report)
    assert [v.subject for v in report.violations] == ["P-2026-10-12-09-buy1"]
    assert "llm:reasoner" in report.violations[0].detail
    assert report.cycles[0].violations == 1 and report.fills == 6


Mutate = Callable[[], AuditEvidence]

VIOLATIONS: dict[str, tuple[Mutate, str]] = {
    "approved by the evaluation actor": (
        lambda: evidence(proposal("P-2026-10-12-01", "system:evaluation")),
        "not a deterministic actor",
    ),
    "assistant with no confirm reference": (
        lambda: evidence(proposal("P-2026-10-12-05", f"assistant:{OWNER}", "approved")),
        "no confirm reference",
    ),
    "assistant direct approve": (
        lambda: evidence(proposal("P-2026-10-12-05", f"assistant:{OWNER}", "approved [assistant, direct]")),
        "no confirm token",
    ),
    "assistant, no bridge record": (
        lambda: evidence(proposal("P-2026-10-12-05", f"assistant:{OWNER}", ASSISTANT_CAUSE)),
        "no bridge record",
    ),
    "assistant, token never spent": (
        lambda: evidence(proposal("P-2026-10-12-05", f"assistant:{OWNER}", ASSISTANT_CAUSE), bridge={REF: bridge_ev(token_spent=False)}),
        "no spent confirm token",
    ),
    "assistant, echo never delivered": (
        lambda: evidence(proposal("P-2026-10-12-05", f"assistant:{OWNER}", ASSISTANT_CAUSE), bridge={REF: bridge_ev(echo_delivered=False)}),
        "never delivered",
    ),
    "assistant confirmed by itself": (
        lambda: evidence(
            proposal("P-2026-10-12-05", f"assistant:{OWNER}", f"approved [assistant-confirmed ref={REF} by=assistant:{OWNER}]"),
            bridge={REF: bridge_ev(confirmed_by=f"assistant:{OWNER}")},
        ),
        "not confirmed by a human",
    ),
    "assistant confirmed by another user": (
        lambda: evidence(
            proposal("P-2026-10-12-05", f"assistant:{OWNER}", "approved [assistant-confirmed ref=" + REF + " by=discord:2]"),
            bridge={REF: bridge_ev(confirmed_by="discord:2")},
        ),
        "not the user it acts for",
    ),
    "assistant confirmed a different command": (
        lambda: evidence(
            proposal("P-2026-10-12-05", f"assistant:{OWNER}", ASSISTANT_CAUSE),
            bridge={REF: bridge_ev(command="APPROVE P-2026-10-12-77", final_command="APPROVE P-2026-10-12-77")},
        ),
        "not this transition",
    ),
    "assistant action mislabelled as the owner": (
        lambda: evidence(proposal("P-2026-10-12-05", f"discord:{OWNER}", ASSISTANT_CAUSE), bridge={REF: bridge_ev()}),
        "recorded under actor",
    ),
    "timer without a delivered alert": (
        lambda: evidence(proposal("P-2026-10-12-03", "system:sweeper", "auto-approved at deadline", mode="auto", receipt=False)),
        "no delivered alert",
    ),
    "timer window opened before delivery": (
        lambda: evidence(
            proposal("P-2026-10-12-03", "system:sweeper", "auto-approved at deadline", mode="auto", window_opened_at=CREATED)
        ),
        "before the alert was delivered",
    ),
    "timer approved before the deadline": (
        lambda: evidence(
            proposal(
                "P-2026-10-12-03", "system:sweeper", "auto-approved at deadline", mode="auto", approved_at=DELIVERED + timedelta(hours=1)
            )
        ),
        "before the veto deadline",
    ),
    "timer on an explicit-mode proposal": (
        lambda: evidence(proposal("P-2026-10-12-03", "system:sweeper", "auto-approved at deadline", mode="explicit")),
        "explicit approval",
    ),
    "fill executed by a non-engine actor": (
        lambda: evidence(proposal("P-2026-10-12-01", "cli", executor="assistant:1")),
        "deterministic engine",
    ),
    "fill with no proposal": (lambda: evidence(extra=[make_trade(1).model_copy(update={"trade_id": "T-0001"})]), "no proposal"),
}


@pytest.mark.parametrize("name", sorted(VIOLATIONS))
def test_gate_fails_on(name: str) -> None:
    build, expected = VIOLATIONS[name]
    report = audit(build())
    assert not report.passed
    assert any(expected in v.detail for v in report.violations), report.violations
    with pytest.raises(AssertionError):
        assert_c1(report)


def test_tampered_chain_is_a_violation() -> None:
    doc = proposal("P-2026-10-12-01", "system:evaluation")
    rec = doc.transitions[2].model_copy(update={"actor": "cli"})  # rewrite the approver without re-hashing
    forged = doc.model_copy(update={"transitions": [*doc.transitions[:2], rec, doc.transitions[3]]})
    report = audit(evidence(forged))
    assert not report.passed and "hash chain broken" in report.violations[0].detail


def test_assistant_veto_without_token_is_a_violation_even_with_no_fill() -> None:
    doc = make_proposal("P-2026-10-12-07", state="PROPOSED", created_at=CREATED, execute_at=EXECUTE_AT)
    doc = advance(doc, "VETOED", DELIVERED, f"assistant:{OWNER}", "vetoed [assistant, direct]")
    report = audit(AuditEvidence([], [doc], {}))
    assert not report.passed and report.fills == 0
    assert report.llm_entries[0].valid is False


def test_assistant_pause_needs_no_token_but_is_still_listed() -> None:
    doc = make_proposal("P-2026-10-12-07", state="PROPOSED", created_at=CREATED, execute_at=EXECUTE_AT)
    doc = advance(doc, "CANCELLED_PAUSED", DELIVERED, f"assistant:{OWNER}", "system paused [assistant, direct]")
    report = audit(AuditEvidence([], [doc], {}))
    assert_c1(report)
    assert len(report.llm_entries) == 1


def test_dispatched_set_with_no_chain_entry_is_listed_and_checked() -> None:
    ok = bridge_ev(command="SET auto_approve on", final_command="SET auto_approve on")
    report = audit(AuditEvidence([], [], {REF: ok}))
    assert_c1(report)
    assert report.llm_entries[0].command == "SET auto_approve on"
    bad = audit(AuditEvidence([], [], {REF: replace(ok, token_spent=False)}))
    assert not bad.passed


def test_report_renders_and_serialises() -> None:
    text = render_report(audit(_llm_executed(traceable_set())))
    assert text.startswith("Guardrail audit (C1): FAIL") and "llm:reasoner" in text and "LLM-mediated actions (1)" in text
    data = json.loads(report_json(audit(traceable_set())))
    assert data["passed"] is True and data["cycles"][0]["by_path"][PATH_TIMER] == 1


# -- end to end through the real bridge -----------------------------------------------------------------------------


class Discord:
    name = "discord"

    def send(self, rendered: Rendered, *, nonce: str) -> Receipt:
        return Receipt("discord", "ext", datetime.now().astimezone(), None)


def _bridge_ctx(world: World) -> CommandContext:
    cfg = Config(discord=DiscordConfig(allowed_user_ids=[OWNER], channel_id="555"))
    return CommandContext(
        store=world.store, cfg=cfg, clock=world.clock, calendar=world.calendar, price_provider=world.yahoo, bridge_key=KEY
    )


def test_assistant_confirmed_approval_audits_as_assistant_actor_not_owner(world: World) -> None:
    pid = "P-2026-10-24-01"
    world.seed()
    now = world.clock()
    exec_at = now + timedelta(hours=2)
    opened = world.put(
        advance(
            make_proposal(pid, state="PROPOSED", created_at=CREATED, execute_at=exec_at, approval_mode="explicit"),
            "VETO_WINDOW",
            DELIVERED,
        )
    )
    ctx = _bridge_ctx(world)
    bridge = Bridge(ctx, KEY)

    result = bridge.propose(f"APPROVE {pid}", OWNER)
    assert result.ref is not None
    deliver_pending(world.store, [Discord()], world.clock)  # type: ignore[list-item]
    token = bridge.confirm(result.ref, Sender("discord", OWNER, None, now))
    assert bridge.dispatch(result.ref, token).startswith("✅")

    approved = world.proposal(pid)
    approval = approved.transitions[-1]
    assert approval.actor == f"assistant:{OWNER}" and approval.actor != f"discord:{OWNER}"
    assert f"ref={result.ref} by=discord:{OWNER}" in approval.cause
    assert opened.state == "VETO_WINDOW" and approved.state == "APPROVED"

    # the deterministic engine's fill, then the audit over what is actually on disk
    world.put(advance(approved, "EXECUTED", exec_at, "system:execution", "filled"))
    world.seed_trades([trade_for(pid).model_copy(update={"timestamp": exec_at})], "50000")
    report = audit_workspace(world.store)
    assert_c1(report)
    assert report.cycles[0].by_path[PATH_ASSISTANT] == 1
    assert report.llm_entries[0].actor == f"assistant:{OWNER}"

    # remove the spent-ref marker: the signed-token evidence is gone, so the same fill now fails C1
    (bridge_dir(world.store.root) / "used" / result.ref).unlink()
    assert not audit_workspace(world.store).passed


def test_load_evidence_reads_nothing_when_the_workspace_is_empty(world: World) -> None:
    ev = load_evidence(world.store)
    assert ev.trades == [] and ev.proposals == [] and ev.bridge == {}


# -- CLI ------------------------------------------------------------------------------------------------------------


def test_cli_audit_exit_codes(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    ws = str(world.root)
    world.seed()  # opening fills only
    assert main(["audit", "--workspace", ws]) == 0
    assert "Guardrail audit (C1): PASS" in capsys.readouterr().out

    stray = make_trade(1).model_copy(update={"trade_id": "T-0001", "shares": Decimal(10)})
    world.seed_trades([stray], "10000")
    assert main(["audit", "--workspace", ws, "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False


def test_cli_audit_missing_workspace(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["audit", "--workspace", str(tmp_path / "nope")]) == 1
    assert "workspace not found" in capsys.readouterr().err

