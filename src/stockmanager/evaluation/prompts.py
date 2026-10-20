"""Reasoner prompt loading (LP §2, §4): versioned files, sha256-checked against ``meta.yaml``, rendered in a sandbox.

Same pattern as the extractor's loader. The system prompt takes its numeric limits from the config snapshot the
builder uses, so the two cannot drift. The user prompt is rendered from ``ReasonerInput`` only; there is no variable
that can hold raw news text.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any

import jinja2
import jinja2.sandbox
import yaml

from stockmanager.config.proposals import ProposalConfig
from stockmanager.evaluation.reasoner_io import ReasonerInput


class ReasonerUnavailable(Exception):
    """The Reasoner cannot run at all (for example a prompt integrity failure): no proposals this cycle."""


class ReasonerPromptIntegrityError(ReasonerUnavailable):
    """A prompt file is missing or does not match the sha256 in ``meta.yaml`` (LP §2.2)."""


def xe(value: str) -> str:
    """Escape ``&``, ``<`` and ``>`` in an untrusted string (LP §2.2). Reasoner input has none; kept for parity."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass(frozen=True)
class ReasonerPrompt:
    """A verified prompt version."""

    version: str
    sha256: str  # over the file hashes; recorded per run in evaluations/*.json.llm
    target_model: str
    system_template: jinja2.Template
    user_template: jinja2.Template
    schema: dict[str, Any]

    def render_system(self, config: ProposalConfig) -> str:
        """Render the system prompt with the four limits from ``config``."""
        return self.system_template.render(
            max_position_pct=config.max_position_pct,
            new_position_max_pct=config.new_position_max_pct,
            proposal_sector_max_pct=config.proposal_sector_max_pct,
            max_trades_per_cycle=config.max_trades_per_cycle,
        )

    def render_user(self, input: ReasonerInput) -> str:
        """Render the user message; each structured block becomes ``tojson(indent=2)`` of the JSON-mode dump."""
        data = input.model_dump(mode="json")
        return self.user_template.render(**data)


def _prompts_root() -> Traversable:
    return files("stockmanager") / "prompts"


def load_reasoner_prompt(root: Traversable | None = None, version: str | None = None) -> ReasonerPrompt:
    """Load ``reasoner/<version>`` (default: the manifest's active version), verifying every file's sha256."""
    base = root if root is not None else _prompts_root()
    try:
        if version is None:
            manifest = yaml.safe_load((base / "manifest.yaml").read_text(encoding="utf-8"))
            version = str(manifest["active"]["reasoner"])
        directory = base / "reasoner" / version
        meta = yaml.safe_load((directory / "meta.yaml").read_text(encoding="utf-8"))
        texts: dict[str, bytes] = {}
        for name, expected in meta["files"].items():
            data = (directory / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ReasonerPromptIntegrityError(f"reasoner/{version}/{name} does not match meta.yaml")
            texts[name] = data
        env = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined, autoescape=False)
        env.filters["xe"] = xe
        digest = hashlib.sha256(
            "".join(f"{n}:{hashlib.sha256(d).hexdigest()}\n" for n, d in sorted(texts.items())).encode()
        )
        return ReasonerPrompt(
            version=version,
            sha256=digest.hexdigest(),
            target_model=str(meta["target_model"]),
            system_template=env.from_string(texts["system.md.j2"].decode("utf-8")),
            user_template=env.from_string(texts["user.md.j2"].decode("utf-8")),
            schema=json.loads(texts["output_schema.json"]),
        )
    except ReasonerUnavailable:
        raise
    except (OSError, KeyError, TypeError, ValueError, AttributeError, yaml.YAMLError, jinja2.TemplateError) as e:
        raise ReasonerPromptIntegrityError(f"cannot load reasoner prompt: {type(e).__name__}") from e
