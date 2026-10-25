"""The triage pipeline (v2 design D1): pre-filter -> classifier -> decision log -> escalation, one pass over a batch.

Order inside ``TriagePipeline.run`` for each (item, held ticker):

1. hard gate (``prefilter``): fail -> ``silent``, the classifier is never called;
2. classifier: ``ClassifierUnavailable`` -> ``classifier_down``, nothing logged, nothing claimed (R4, fail closed);
3. confidence gate: below ``triage.confidence_threshold`` -> ``silent``;
4. citation: no usable source URL -> ``blocked_no_citation`` (A5);
5. shadow mode / benchmark not passed -> ``shadow_escalate``: recorded, and NOTHING else happens (no dedupe claim, no
   wake, no scheduler contact), so switching production on later is not pre-empted by shadow-era keys;
6. daily cap -> ``suppressed_cap``;
7. dedupe: ``FileStore.claim_dedupe_key((ticker, event_hash))`` BEFORE dispatch -> ``suppressed_duplicate`` on a repeat
   (atomic-file store, not SQLite: it survives the event db being down, R4);
8. quiet hours -> ``defer_digest`` with ``due_at`` = end of quiet hours (R5), else ``escalate`` with ``due_at`` = now.

An escalation is only ever a ``TriageWake`` in the wake queue. This package has no import or call edge to the
execution engine (A2): the tick turns a due wake into a ``triage_escalation`` DueJob.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from stockmanager.config.triage import TriageConfig
from stockmanager.store import FileStore
from stockmanager.triage.classifier import ClassifierUnavailable, TriageClassifier
from stockmanager.triage.decision_log import DecisionLog
from stockmanager.triage.models import Action, Decision, EventKind, TriageItem, has_citation
from stockmanager.triage.prefilter import prefilter
from stockmanager.triage.quiet import QuietHours
from stockmanager.triage.wakes import TriageWake, WakeQueue, cycle_id_for

log = logging.getLogger(__name__)

DEDUPE_PREFIX = "triage:"
_WORDS = re.compile(r"[a-z0-9]+")


def event_hash(ticker: str, kind: EventKind, title: str, event_id: str | None = None) -> str:
    """Stable id of an event: the classifier's ``event_id`` if it named one, else the normalised headline.

    Two outlets running the same headline collapse to one event; differently worded stories about one event do not
    unless the classifier supplies a canonical ``event_id``.
    """
    basis = event_id if event_id else " ".join(_WORDS.findall(title.lower()))
    return hashlib.sha256(f"{ticker}|{kind.value}|{basis}".encode()).hexdigest()


def dedupe_key(ticker: str, ev_hash: str) -> str:
    """The ledger key for ``(ticker, event_hash)``; prefixed so it never meets an outbox key."""
    return f"{DEDUPE_PREFIX}{ticker}:{ev_hash}"


@dataclass(frozen=True)
class TriageRun:
    """Result of one pass: every decision (``classifier_down`` ones are not logged) and the items to retry later."""

    decisions: tuple[Decision, ...]
    stalled: frozenset[str]  # item_hash of items that could not be decided; they must stay in the backlog


class TriagePipeline:
    """One triage pass. Holds no state of its own: dedupe, wakes and decisions all live in the workspace."""

    def __init__(
        self, store: FileStore, cfg: TriageConfig, classifier: TriageClassifier, held: Collection[str]
    ) -> None:
        self._store = store
        self._cfg = cfg
        self._classifier = classifier
        self._held = frozenset(held)
        self._wakes = WakeQueue(store.root)
        self._log = DecisionLog(store.root)
        self._quiet = QuietHours.parse(cfg.quiet_hours)

    def run(self, items: Sequence[TriageItem], now: datetime) -> TriageRun:
        """Triage ``items`` as of ``now`` (PT). Never raises for a classifier or disk failure: the item stays stalled."""
        decisions: list[Decision] = []
        stalled: set[str] = set()
        down = False
        for item in items:
            try:
                got = self._triage_item(item, now, down)
            except OSError:
                log.exception("triage could not enqueue a wake; item %s stays in the backlog", item.item_hash)
                stalled.add(item.item_hash)
                continue
            if any(d.action == "classifier_down" for d in got):
                down = True  # fail closed for the rest of the batch without waiting on a dead model again
                stalled.add(item.item_hash)
            decisions.extend(got)
        self._log.record([d for d in decisions if d.action != "classifier_down"])
        return TriageRun(tuple(decisions), frozenset(stalled))

    def _triage_item(self, item: TriageItem, now: datetime, down: bool) -> list[Decision]:
        gate = prefilter(item, self._held)
        if not gate.passed:
            ticker = gate.tickers[0] if gate.tickers else ""
            if not ticker:
                return []  # names no held ticker: nothing to decide about, nothing to log
            return [self._decision(now, ticker, _plain_hash(ticker, item), False, None, "silent", item.url)]
        out: list[Decision] = []
        for ticker in gate.tickers:
            try:
                if down:
                    raise ClassifierUnavailable("classifier already failed in this batch")
                verdict = self._classifier.classify(item, ticker, gate.kinds)
            except ClassifierUnavailable:
                out.append(self._decision(now, ticker, _plain_hash(ticker, item), True, None, "classifier_down", item.url))
                return out
            ev = event_hash(ticker, verdict.event_kind, item.title, verdict.event_id)
            out.append(self._after_classification(item, ticker, ev, verdict.confidence, now))
        return out

    def _after_classification(
        self, item: TriageItem, ticker: str, ev: str, confidence: float, now: datetime
    ) -> Decision:
        def make(action: Action, due_at: datetime | None = None) -> Decision:
            return self._decision(now, ticker, ev, True, confidence, action, item.url, due_at)

        if confidence < self._cfg.confidence_threshold:
            return make("silent")
        if not has_citation(item.url):
            return self._decision(now, ticker, ev, True, confidence, "blocked_no_citation", None)
        if not self._cfg.escalation_live:
            return make("shadow_escalate")
        if self._wakes.created_on(now.date()) >= self._cfg.max_escalations_per_day:
            return make("suppressed_cap")
        window = timedelta(hours=self._cfg.dedupe_window_hours)
        key = dedupe_key(ticker, ev)
        if not self._store.claim_dedupe_key(key, now, window):
            return make("suppressed_duplicate")
        due_at = self._quiet.next_end(now)
        wake = TriageWake(cycle_id_for(now, ticker, ev), ticker, ev, item.url, now, due_at)
        try:
            self._wakes.enqueue(wake)
        except OSError:
            self._store.release_dedupe_key(key)  # not enqueued: free the key so the backlog retry can escalate
            raise
        return make("defer_digest" if due_at > now else "escalate", due_at)

    @staticmethod
    def _decision(
        now: datetime,
        ticker: str,
        ev: str,
        hard_gate: bool,
        confidence: float | None,
        action: Action,
        url: str | None,
        due_at: datetime | None = None,
    ) -> Decision:
        return Decision(now, ticker, ev, hard_gate, confidence, action, url, due_at)


def _plain_hash(ticker: str, item: TriageItem) -> str:
    """Event hash for an item that never reached the classifier: derived from the item identity."""
    return hashlib.sha256(f"{ticker}|{item.item_hash}".encode()).hexdigest()
