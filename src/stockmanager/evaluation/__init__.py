"""Evaluation: the Reasoner seam, its contracts and prompts, and the ProposalBuilder (AS §1.2, §8)."""

from stockmanager.evaluation.builder import BuildResult, ProposalBuilder
from stockmanager.evaluation.prompts import (
    ReasonerPrompt,
    ReasonerPromptIntegrityError,
    ReasonerUnavailable,
    load_reasoner_prompt,
)
from stockmanager.evaluation.reasoner import (
    AnthropicReasoner,
    Reasoner,
    ReasonerFailure,
    UnavailableReasoner,
    build_reasoner,
    parse_output,
)
from stockmanager.evaluation.reasoner_io import ReasonerInput, ReasonerOutput

__all__ = [
    "AnthropicReasoner",
    "BuildResult",
    "ProposalBuilder",
    "Reasoner",
    "ReasonerFailure",
    "ReasonerInput",
    "ReasonerOutput",
    "ReasonerPrompt",
    "ReasonerPromptIntegrityError",
    "ReasonerUnavailable",
    "UnavailableReasoner",
    "build_reasoner",
    "load_reasoner_prompt",
    "parse_output",
]
