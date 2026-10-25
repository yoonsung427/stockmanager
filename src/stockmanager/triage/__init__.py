"""Triage tier (v2 design D1): raw-text pre-filter, pluggable classifier, decision log, escalation via the scheduler.

Never imports the execution engine (A2): an escalation is a wake file that the tick dispatches as a
``triage_escalation`` DueJob, and nothing here proposes, approves or fills anything.
"""

from __future__ import annotations

from stockmanager.triage.classifier import ClassifierUnavailable, TriageClassifier, UnavailableClassifier
from stockmanager.triage.models import Classification, Decision, EventKind, TriageItem
from stockmanager.triage.pipeline import TriagePipeline, TriageRun
from stockmanager.triage.prefilter import PreFilterResult, prefilter
from stockmanager.triage.wakes import TriageWake, WakeQueue

__all__ = [
    "Classification",
    "ClassifierUnavailable",
    "Decision",
    "EventKind",
    "PreFilterResult",
    "TriageClassifier",
    "TriageItem",
    "TriagePipeline",
    "TriageRun",
    "TriageWake",
    "UnavailableClassifier",
    "WakeQueue",
    "prefilter",
]
