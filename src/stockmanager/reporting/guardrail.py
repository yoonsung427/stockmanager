"""Guardrail audit (v2 M5, proving C1): every fill traces to a deterministic actor, never to an LLM alone.

``audit`` is a pure function over stored records (fills, hash-chained proposal transitions, the assistant bridge's
decision records); ``load_evidence`` reads them from the workspace. Nothing here calls an LLM or the network, and
nothing reads SQLite (D7): the proof rests on the atomic-file store.

A fill is traceable when its proposal's chain shows a deterministic execution (``system:execution``, from
``APPROVED``) and an approval by one of:

* ``cli`` or ``discord:<uid>``: a deterministic verb. A button click dispatches the same verb under the same actor,
  so the chain cannot tell a click from a typed command; both count as ``verb_or_button``.
* ``system:sweeper``, the timer: valid only for an ``auto`` proposal with a delivery receipt whose deadline is the
  proposal's, a ``VETO_WINDOW`` entry no earlier than that delivery, and an approval and fill no earlier than the
  deadline (the window opens only after delivery).
* ``assistant:<uid>`` (R2): LLM-mediated, so counted and listed apart. Valid only with evidence of a signed confirm
  token: a bridge record for the ref named in the transition cause, dispatched, confirmed by a human actor (never
  the assistant) who is the user it acts for, with the echo delivered on Discord and the single-use ref spent (the
  handler spends it only after verifying the token's signature). The bridge never stores the token itself.
  A direct ``PAUSE`` is the one allow-listed assistant action that needs no token (it only reduces risk).

Opening fills (``P-OPEN-<date>``) have no proposal; they run only while the portfolio is ``APPROVED`` by
``APPROVE OPENING``, which records no actor. They are counted as ``opening`` and the gap is stated in the report.

Anything else is a violation: no proposal, a broken chain, an approval by an unlisted actor, an assistant action
without verifiable confirmation, or a ``[assistant`` cause under a non-assistant actor (a mislabelled actor).
Cycles are keyed by the fill's PT date, because a proposal stores no ``cycle_id`` (see ``reporting.py``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from stockmanager.commands.confirm import bridge_dir, is_consumed, read_record
from stockmanager.domain.docs import ProposalDoc, Trade, TransitionRecord
from stockmanager.domain.proposal_state import GENESIS_HASH, can_transition, chain_hash
from stockmanager.notify.outbox import Alert
from stockmanager.store import FileStore

PT = ZoneInfo("America/Los_Angeles")

PATH_VERB = "verb_or_button"
PATH_TIMER = "timer"
PATH_ASSISTANT = "assistant_confirmed"
PATH_OPENING = "opening"
PATHS: tuple[str, ...] = (PATH_VERB, PATH_TIMER, PATH_ASSISTANT, PATH_OPENING)

EXECUTION_ACTOR = "system:execution"
TIMER_ACTOR = "system:sweeper"
OPENING_PREFIX = "P-OPEN-"
UNATTRIBUTED = "unattributed"

#: The verb each irreversible terminal transition must have been issued as, for an assistant-driven one.
_VERB_FOR_STATE = {"APPROVED": "APPROVE", "VETOED": "VETO", "CANCELLED_PAUSED": "PAUSE", "CANCELLED_CLOSED": "CLOSE"}
_CONFIRMED_RE = re.compile(r"\[assistant-confirmed ref=([0-9a-f]{16}) by=([^\]]*)\]")
_DIRECT = "[assistant, direct]"

NOTES: tuple[str, ...] = (
    "A button click and a typed command share one actor in the chain, so they are reported together.",
    "Opening fills have no proposal; APPROVE OPENING records no actor, so they are counted but not attributed.",
    "Cycles are the fill's PT date: a proposal stores no cycle_id.",
)


@dataclass(frozen=True)
class BridgeEvidence:
    """What the bridge stored about one assistant proposal (``state/bridge/<ref>.json``) plus two outbox/marker facts."""

    ref: str
    uid: str
    command: str
    state: str
    confirmed_by: str | None
    final_command: str | None
    token_spent: bool  # the single-use ref was consumed, which the handler does only after verifying the signature
    echo_delivered: bool  # the echo alert reads back as sent on Discord


@dataclass(frozen=True)
class AuditEvidence:
    """Everything ``audit`` reads."""

    trades: Sequence[Trade]
    proposals: Sequence[ProposalDoc]
    bridge: Mapping[str, BridgeEvidence]


@dataclass(frozen=True)
class LlmEntry:
    """One LLM-mediated action, as logged: who acted, on what, and who confirmed it."""

    actor: str
    subject: str  # proposal id and transition seq, or the bridge ref for an action with no chain entry
    command: str
    confirmed_by: str | None
    ref: str | None
    valid: bool


@dataclass(frozen=True)
class Violation:
    """A breach of C1. ``cycle`` is the fill's PT date, or ``unattributed`` when no fill is involved."""

    cycle: str
    subject: str
    detail: str


@dataclass(frozen=True)
class CycleSummary:
    """Fills of one cycle counted by trace path, plus its LLM-mediated and violation counts."""

    cycle: str
    fills: int
    by_path: Mapping[str, int]
    llm_mediated: int
    violations: int


@dataclass(frozen=True)
class AuditReport:
    """The C1 proof: ``passed`` only when there is not a single violation."""

    cycles: tuple[CycleSummary, ...]
    llm_entries: tuple[LlmEntry, ...]
    violations: tuple[Violation, ...]

    @property
    def passed(self) -> bool:
        """True when no fill (and no assistant action) breaks C1."""
        return not self.violations

    @property
    def fills(self) -> int:
        """Total fills audited."""
        return sum(c.fills for c in self.cycles)


# -- loading --------------------------------------------------------------------------------------------------------


def _echo_delivered(root: Path, alert_id: str) -> bool:
    for box in ("sent", "pending"):
        path = root / "alerts" / box / f"{alert_id}.json"
        if not path.exists():
            continue
        try:
            state = Alert.from_json(path.read_text(encoding="utf-8")).channels.get("discord")
        except (OSError, ValueError, KeyError):
            return False
        return state is not None and state.state == "sent"
    return False


def load_bridge(root: Path) -> dict[str, BridgeEvidence]:
    """Every readable bridge decision record under ``root``, keyed by ref. Read-only."""
    directory = bridge_dir(root)
    out: dict[str, BridgeEvidence] = {}
    if not directory.exists():
        return out
    for path in sorted(directory.glob("*.json")):
        record = read_record(root, path.stem)
        if record is None:
            continue
        by = record.get("confirmed_by")
        final = record.get("final_command")
        out[path.stem] = BridgeEvidence(
            ref=path.stem,
            uid=str(record.get("uid", "")),
            command=str(record.get("command", "")),
            state=str(record.get("state", "")),
            confirmed_by=by if isinstance(by, str) else None,
            final_command=final if isinstance(final, str) else None,
            token_spent=is_consumed(root, path.stem),
            echo_delivered=_echo_delivered(root, str(record.get("echo_alert_id", ""))),
        )
    return out


def load_evidence(store: FileStore) -> AuditEvidence:
    """Read fills, proposals and bridge records from the file store (no lock: every file is written atomically)."""
    trades = store.load_portfolio().trades if store.portfolio_exists() else []
    return AuditEvidence(trades, store.load_proposals(), load_bridge(store.root))


# -- checks ---------------------------------------------------------------------------------------------------------


def _chain_problem(doc: ProposalDoc) -> str | None:
    prev_hash, prev_state = GENESIS_HASH, None
    for index, rec in enumerate(doc.transitions, start=1):
        if rec.seq != index or rec.prev_hash != prev_hash or rec.from_state != prev_state:
            return f"hash chain broken at seq {index}"
        if rec.hash != chain_hash(prev_hash, rec.model_dump(mode="json", by_alias=True)):
            return f"hash chain broken at seq {index}"
        if prev_state is not None and not can_transition(prev_state, rec.to):
            return f"illegal transition {prev_state} -> {rec.to} at seq {index}"
        prev_hash, prev_state = rec.hash, rec.to
    return None


def _record_problems(ev: BridgeEvidence | None, ref: str) -> list[str]:
    if ev is None:
        return [f"no bridge record for ref {ref}"]
    problems: list[str] = []
    if ev.state != "dispatched":
        problems.append(f"bridge record {ref} is {ev.state!r}, not dispatched")
    if ev.confirmed_by is None or not (ev.confirmed_by == "cli" or ev.confirmed_by.startswith("discord:")):
        problems.append(f"bridge record {ref} was not confirmed by a human actor (confirmed_by={ev.confirmed_by!r})")
    elif ev.confirmed_by.startswith("discord:") and ev.confirmed_by != f"discord:{ev.uid}":
        problems.append(f"bridge record {ref} was confirmed by {ev.confirmed_by}, not the user it acts for")
    if not ev.token_spent:
        problems.append(f"bridge record {ref} has no spent confirm token")
    if not ev.echo_delivered:
        problems.append(f"bridge record {ref} echo was never delivered")
    if ev.final_command != ev.command:
        problems.append(f"bridge record {ref} ran a command other than the one confirmed")
    return problems


def _transition_problems(doc: ProposalDoc, rec: TransitionRecord, bridge: Mapping[str, BridgeEvidence]) -> list[str]:
    """Why an ``assistant:<uid>`` chain entry is not backed by a signed, human-confirmed token (empty: it is)."""
    uid = rec.actor.removeprefix("assistant:")
    if _DIRECT in rec.cause:
        # Only PAUSE skips the token (INSTANT_VERBS): it cancels proposals and can never cause a fill.
        return [] if rec.to == "CANCELLED_PAUSED" else [f"assistant moved the proposal to {rec.to} with no confirm token"]
    match = _CONFIRMED_RE.search(rec.cause)
    if match is None:
        return ["assistant action carries no confirm reference"]
    ref, by = match.group(1), match.group(2)
    ev = bridge.get(ref)
    problems = _record_problems(ev, ref)
    if ev is None:
        return problems
    if ev.uid != uid:
        problems.append(f"bridge record {ref} is for user {ev.uid}, the chain says {uid}")
    if by != ev.confirmed_by:
        problems.append(f"chain says confirmed by {by}, bridge record says {ev.confirmed_by}")
    words = ev.command.upper().split()
    if not words or words[0] != _VERB_FOR_STATE.get(rec.to) or (rec.to in ("APPROVED", "VETOED") and doc.proposal_id.upper() not in words[1:2]):
        problems.append(f"bridge record {ref} confirmed {ev.command!r}, not this transition")
    return problems


def _timer_problems(doc: ProposalDoc, approval: TransitionRecord, trade: Trade) -> list[str]:
    deadline = doc.veto_deadline
    receipts = [r for r in doc.delivery.receipts if deadline is not None and r.veto_deadline == deadline]
    if doc.approval_mode != "auto":
        return ["timer approved a proposal that needs an explicit approval"]
    if deadline is None or not receipts:
        return ["timer approval has no delivered alert behind its veto deadline"]
    opened = next((t for t in doc.transitions if t.to == "VETO_WINDOW"), None)
    delivered = min(r.delivered_at for r in receipts)
    if opened is None or opened.actor != TIMER_ACTOR or opened.at < delivered:
        return ["the veto window opened before the alert was delivered"]
    if approval.at < deadline or trade.timestamp < deadline:
        return ["timer approved or filled before the veto deadline"]
    return []


def audit(evidence: AuditEvidence) -> AuditReport:
    """Prove C1 over ``evidence``: count fills by trace path, list LLM-mediated actions, collect violations."""
    docs = {d.proposal_id: d for d in evidence.proposals}
    by_trade = {leg.trade_id: d for d in evidence.proposals for leg in d.legs}
    violations: list[Violation] = []
    entries: list[LlmEntry] = []
    problems_for: dict[tuple[str, int], list[str]] = {}
    seen_refs: set[str] = set()
    chain_problem = {pid: _chain_problem(d) for pid, d in docs.items()}

    def violate(cycle: str, subject: str, detail: str) -> None:
        violations.append(Violation(cycle, subject, detail))

    # 1. Every assistant action in any chain, fill or not: a veto or close is irreversible too (C1).
    for doc in evidence.proposals:
        for rec in doc.transitions:
            subject = f"{doc.proposal_id}#{rec.seq}"
            if not rec.actor.startswith("assistant:"):
                if "[assistant" in rec.cause:
                    violate(UNATTRIBUTED, subject, f"assistant-confirmed cause recorded under actor {rec.actor}")
                continue
            problems = _transition_problems(doc, rec, evidence.bridge)
            problems_for[(doc.proposal_id, rec.seq)] = problems
            match = _CONFIRMED_RE.search(rec.cause)
            ref = match.group(1) if match else None
            ev = evidence.bridge.get(ref) if ref else None
            if ref:
                seen_refs.add(ref)
            entries.append(
                LlmEntry(rec.actor, subject, ev.command if ev else f"-> {rec.to}", match.group(2) if match else None, ref, not problems)
            )
            for problem in problems:
                violate(UNATTRIBUTED, subject, problem)

    # 2. Dispatched assistant actions with no chain entry (SET, CLOSE confirm, ...): the record is the only trace.
    for ref, ev in sorted(evidence.bridge.items()):
        if ev.state != "dispatched" or ref in seen_refs:
            continue
        problems = _record_problems(ev, ref)
        entries.append(LlmEntry(f"assistant:{ev.uid}", ref, ev.command, ev.confirmed_by, ref, not problems))
        for problem in problems:
            violate(UNATTRIBUTED, ref, problem)

    # 3. Every fill.
    counts: dict[str, dict[str, int]] = {}
    for trade in evidence.trades:
        cycle = trade.timestamp.astimezone(PT).date().isoformat()
        row = counts.setdefault(cycle, dict.fromkeys(PATHS, 0))
        path, fault = _trace(trade, by_trade.get(trade.trade_id), chain_problem, problems_for)
        if fault is not None:
            violate(cycle, trade.trade_id, fault)
        else:
            row[path] += 1
    return _summarise(counts, entries, violations, evidence.trades)


def _trace(
    trade: Trade,
    doc: ProposalDoc | None,
    chain_problem: Mapping[str, str | None],
    problems_for: Mapping[tuple[str, int], list[str]],
) -> tuple[str, str | None]:
    """The trace path of ``trade``, or ``("", reason)`` if it does not trace to a deterministic actor."""
    if doc is None:
        if trade.trade_id.startswith(OPENING_PREFIX):
            return PATH_OPENING, None
        return "", "no proposal produced this fill"
    if (broken := chain_problem[doc.proposal_id]) is not None:
        return "", f"{doc.proposal_id}: {broken}"
    executed = next((t for t in doc.transitions if t.to == "EXECUTED"), None)
    if executed is None or executed.actor != EXECUTION_ACTOR or executed.from_state != "APPROVED":
        return "", f"{doc.proposal_id} was never executed by the deterministic engine"
    approval = next((t for t in doc.transitions if t.to == "APPROVED"), None)
    if approval is None:
        return "", f"{doc.proposal_id} was filled without an approval"
    actor = approval.actor
    if actor == "cli" or actor.startswith("discord:"):
        return PATH_VERB, None
    if actor == TIMER_ACTOR:
        timer = _timer_problems(doc, approval, trade)
        return (PATH_TIMER, None) if not timer else ("", "; ".join(timer))
    if actor.startswith("assistant:"):
        problems = problems_for.get((doc.proposal_id, approval.seq), ["assistant approval was not checked"])
        if problems:
            return "", "traceable only to an unconfirmed assistant action: " + "; ".join(problems)
        return PATH_ASSISTANT, None
    return "", f"approval by {actor!r}, which is not a deterministic actor"


def _summarise(
    counts: Mapping[str, Mapping[str, int]],
    entries: Iterable[LlmEntry],
    violations: Sequence[Violation],
    trades: Sequence[Trade],
) -> AuditReport:
    fills: dict[str, int] = {}
    for trade in trades:
        key = trade.timestamp.astimezone(PT).date().isoformat()
        fills[key] = fills.get(key, 0) + 1
    cycles = tuple(
        CycleSummary(
            cycle=cycle,
            fills=fills[cycle],
            by_path=dict(counts.get(cycle, dict.fromkeys(PATHS, 0))),
            llm_mediated=counts.get(cycle, {}).get(PATH_ASSISTANT, 0),
            violations=sum(1 for v in violations if v.cycle == cycle),
        )
        for cycle in sorted(fills)
    )
    return AuditReport(cycles, tuple(entries), tuple(violations))


# -- rendering ------------------------------------------------------------------------------------------------------


def render_report(report: AuditReport) -> str:
    """Plain-text report: verdict, per-cycle table, the LLM-mediated audit entries, then every violation."""
    unattributed = sum(1 for v in report.violations if v.cycle == UNATTRIBUTED)
    lines = [
        f"Guardrail audit (C1): {'PASS' if report.passed else 'FAIL'} - {report.fills} fills, "
        + f"{len(report.llm_entries)} LLM-mediated actions, {len(report.violations)} violations",
        "",
        f"{'cycle':<12}{'fills':>6}{PATH_VERB:>16}{PATH_TIMER:>8}{PATH_ASSISTANT:>21}{PATH_OPENING:>9}{'violations':>12}",
    ]
    for c in report.cycles:
        paths = "".join(f"{c.by_path[p]:>{w}}" for p, w in zip(PATHS, (16, 8, 21, 9), strict=True))
        lines.append(f"{c.cycle:<12}{c.fills:>6}{paths}{c.violations:>12}")
    if not report.cycles:
        lines.append("(no fills)")
    lines += ["", f"LLM-mediated actions ({len(report.llm_entries)}):"]
    lines += [
        f"  {e.actor} {e.subject} {e.command!r} confirmed_by={e.confirmed_by} ref={e.ref} {'ok' if e.valid else 'UNVERIFIED'}"
        for e in report.llm_entries
    ] or ["  none"]
    lines += ["", f"Violations ({len(report.violations)}, {unattributed} not tied to a fill):"]
    lines += [f"  [{v.cycle}] {v.subject}: {v.detail}" for v in report.violations] or ["  none"]
    lines += ["", *(f"Note: {n}" for n in NOTES)]
    return "\n".join(lines)


def report_json(report: AuditReport) -> str:
    """Machine-readable form of ``report`` (canonical: ``indent=2``, ``ensure_ascii=False``, trailing newline)."""
    body = {
        "passed": report.passed,
        "fills": report.fills,
        "cycles": [
            {"cycle": c.cycle, "fills": c.fills, "by_path": dict(c.by_path), "llm_mediated": c.llm_mediated, "violations": c.violations}
            for c in report.cycles
        ],
        "llm_entries": [e.__dict__ for e in report.llm_entries],
        "violations": [v.__dict__ for v in report.violations],
    }
    return json.dumps(body, indent=2, ensure_ascii=False) + "\n"


def audit_workspace(store: FileStore) -> AuditReport:
    """Load the evidence under ``store`` and audit it."""
    return audit(load_evidence(store))


__all__ = [
    "PATHS",
    "AuditEvidence",
    "AuditReport",
    "BridgeEvidence",
    "CycleSummary",
    "LlmEntry",
    "Violation",
    "audit",
    "audit_workspace",
    "load_bridge",
    "load_evidence",
    "render_report",
    "report_json",
]
