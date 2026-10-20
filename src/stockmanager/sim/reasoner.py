"""``StubReasoner``: a scripted ``Reasoner`` that spies on its input (TS §0.3)."""

from __future__ import annotations

from stockmanager.evaluation.reasoner import ReasonerFailure
from stockmanager.evaluation.reasoner_io import ReasonerInput, ReasonerOutput


class StubReasoner:
    """Returns ``output`` (or raises ``failure``) and records every ``ReasonerInput`` it was given."""

    def __init__(self, output: ReasonerOutput, failure: ReasonerFailure | None = None) -> None:
        self.output = output
        self.failure = failure
        self.inputs: list[ReasonerInput] = []

    def verify(self) -> None:
        """Always available."""

    def evaluate(self, input: ReasonerInput) -> ReasonerOutput:
        """Record ``input``; return the scripted output or raise the scripted failure."""
        self.inputs.append(input)
        if self.failure is not None:
            raise self.failure
        return self.output
