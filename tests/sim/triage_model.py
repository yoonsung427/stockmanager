"""``FixedTriageModel``: a deterministic ``TriageClassifier`` with scripted outputs (v2 test plan §0, A1).

Fixed output per ``item_hash`` (or a default), a ``down`` switch like ``RecordingChannel.down``, and a record of every
call so tests can assert that the pre-filter kept the model out of the loop. No randomness, no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from stockmanager.triage.classifier import ClassifierUnavailable
from stockmanager.triage.models import Classification, EventKind, TriageItem

SILENT = Classification(EventKind.EARNINGS, 0.0)  # below any sane threshold


class FixedTriageModel:
    """Returns ``outputs[item.item_hash]`` or ``default``; raises ``ClassifierUnavailable`` while ``down``."""

    def __init__(
        self,
        default: Classification = SILENT,
        outputs: Mapping[str, Classification] | None = None,
    ) -> None:
        self.default = default
        self.outputs = dict(outputs or {})
        self.down = False
        self.calls: list[tuple[str, str]] = []  # (item_hash, ticker), in call order

    def classify(self, item: TriageItem, ticker: str, hinted: Sequence[EventKind]) -> Classification:
        """Record the call; return the scripted verdict or raise while ``down``."""
        self.calls.append((item.item_hash, ticker))
        if self.down:
            raise ClassifierUnavailable("simulated classifier outage")
        return self.outputs.get(item.item_hash, self.default)
