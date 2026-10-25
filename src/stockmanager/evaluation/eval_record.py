"""The persisted evaluation record, ``evaluations/eval-<cycle_id>.json``: the one writer and the one reader.

The file holds the ``ReasonerOutput`` fields at the top level (so the dashboard's summary reader sees ``summary`` and
``ideas``) plus metadata: ``eval_id``, ``cycle_id``, ``cycle_date``, the Reasoner prompt ``prompt_sha256`` pin (null for a
Reasoner with no pinned prompt), ``proposal_ideas`` (proposal id -> the ``priority`` of the idea it came from) and
``proposals_count``. The ids an idea or holding cites in ``evidence_fact_ids`` are ``fact_id``s of ``extracted-facts/``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from stockmanager.evaluation.reasoner_io import ReasonerOutput
from stockmanager.store import canonical_json

EVAL_META_KEYS = frozenset(
    {"eval_id", "cycle_id", "cycle_date", "prompt_sha256", "proposal_ideas", "proposals_count"}
)


@dataclass(frozen=True)
class StoredEvaluation:
    """One persisted Reasoner run: its id, its output, which proposal came from which idea, and its prompt pin."""

    eval_id: str
    output: ReasonerOutput
    proposal_ideas: Mapping[str, int]
    prompt_sha256: str | None = None


def eval_file_name(cycle_id: str) -> str:
    """``eval-<cycle_id>.json``; the stem is the eval id."""
    return f"eval-{cycle_id}.json"


def dump_evaluation(
    cycle_id: str,
    cycle_date: date,
    output: ReasonerOutput,
    proposal_ideas: Mapping[str, int],
    prompt_sha256: str | None,
) -> str:
    """Canonical JSON text of one evaluation record."""
    doc = {
        "eval_id": f"eval-{cycle_id}",
        "cycle_id": cycle_id,
        "cycle_date": cycle_date.isoformat(),
        "prompt_sha256": prompt_sha256,
        "proposal_ideas": dict(proposal_ideas),
        "proposals_count": len(proposal_ideas),
        **output.model_dump(mode="json"),
    }
    return canonical_json(doc)


def parse_evaluation(text: str, default_id: str) -> StoredEvaluation:
    """Parse a record. Raises ``ValueError``/``TypeError``/``AttributeError`` on anything malformed."""
    raw = json.loads(text)
    eval_id = str(raw.get("eval_id", default_id))
    links = {str(k): int(v) for k, v in dict(raw.get("proposal_ideas", {})).items()}
    sha = raw.get("prompt_sha256")
    body = {k: v for k, v in raw.items() if k not in EVAL_META_KEYS}
    output = ReasonerOutput.model_validate_json(json.dumps(body))
    return StoredEvaluation(eval_id, output, links, None if sha is None else str(sha))
