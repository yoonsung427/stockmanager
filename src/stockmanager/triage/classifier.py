"""Classifier port (v2 design D1). The production model is pending the Q1 benchmark; only this interface is fixed.

``tests/sim/triage_model.py`` holds the deterministic double. ``UnavailableClassifier`` is the production placeholder:
until a model is chosen, every call fails closed (R4), so nothing can escalate.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from stockmanager.triage.models import Classification, EventKind, TriageItem


class ClassifierUnavailable(Exception):
    """The classifier cannot answer right now (down, timed out, no model configured). Fail closed (R4)."""


class TriageClassifier(Protocol):
    """Judge one pre-filtered item for one held ticker. May raise ``ClassifierUnavailable``; must not escalate."""

    def classify(self, item: TriageItem, ticker: str, hinted: Sequence[EventKind]) -> Classification:
        """Return the event kind and a confidence in ``[0, 1]`` for ``ticker``'s relation to ``item``."""
        ...


class UnavailableClassifier:
    """No model chosen yet: always raises ``ClassifierUnavailable``."""

    def classify(self, item: TriageItem, ticker: str, hinted: Sequence[EventKind]) -> Classification:
        """Always raises."""
        raise ClassifierUnavailable("no triage classifier configured (pending the Q1 benchmark decision)")
