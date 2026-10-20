"""Extraction (LLM seam 1, AS §7.4, news-sources §4): one raw item per call, never batched.

This is the injection boundary. The model sees one headline and snippet and may return only ``ticker``, ``event``,
``description`` and ``date``. It never sees or supplies a URL or domain. Failures are per item (a reason code and no
facts) unless the extractor as a whole is down (``ExtractorHealth``), which makes the cycle's news unavailable.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any, Literal, Protocol, cast

import anthropic
import jinja2
import jinja2.sandbox
import yaml
from pydantic import BaseModel, ValidationError

from stockmanager.domain.docs import NewsEvent
from stockmanager.news import Rejection
from stockmanager.news.config import NewsConfig
from stockmanager.news.normalise import PreparedItem

log = logging.getLogger(__name__)

ExtractReason = Literal[
    "extract_refusal",
    "extract_max_tokens",
    "extract_schema",
    "extract_timeout",
    "extract_infra",
    "extract_deadline",
]

MAX_CANDIDATES = 5
MIN_ATTEMPTS_FOR_DOWN = 10
DOWN_FAILURE_RATE = 0.5


class Candidate(BaseModel, extra="forbid"):
    """What the model may return per fact (AS §7.4). Untrusted until ``validate`` accepts it."""

    ticker: str
    event: NewsEvent
    description: str
    date: dt.date


class ExtractFailure(Exception):
    """One item's extraction failed. ``infra`` failures (timeouts, connection, 5xx, 429, auth) feed ``ExtractorHealth``."""

    def __init__(self, reason: ExtractReason, *, infra: bool = False, auth: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.infra = infra
        self.auth = auth


class ExtractorUnavailable(Exception):
    """The extractor cannot run at all (for example a prompt integrity failure): news is unavailable."""


class PromptIntegrityError(ExtractorUnavailable):
    """A prompt file is missing or does not match the sha256 in ``meta.yaml`` (LP §2.2)."""


@dataclass(frozen=True)
class ExtractContext:
    """The trusted variables of the user message; identical for every item of a cycle."""

    cycle_date: dt.date
    benchmark: str
    allowed_tickers: tuple[str, ...]


class Extractor(Protocol):
    """Turns one prepared item into candidates."""

    def verify(self) -> None:
        """Raise ``ExtractorUnavailable`` if the extractor cannot run (checked once, before fetching)."""
        ...

    def extract(self, item: PreparedItem, ctx: ExtractContext) -> list[Candidate]:
        """Return the item's candidates (possibly none). Raises ``ExtractFailure``."""
        ...


# -- prompt files ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractorPrompt:
    """A verified prompt version (LP §2)."""

    version: str
    sha256: str  # over the three file hashes; recorded per run
    target_model: str
    system: str
    user_template: jinja2.Template
    schema: dict[str, Any]

    def render_user(self, item: PreparedItem, ctx: ExtractContext) -> str:
        """Render the user message. Only ``title`` and ``snippet`` are untrusted and both pass the ``xe`` filter."""
        raw = item.item
        return self.user_template.render(
            cycle_date=ctx.cycle_date.isoformat(),
            benchmark=ctx.benchmark,
            allowed_tickers=list(ctx.allowed_tickers),
            focus_ticker=item.focus_ticker,
            published=raw.published.isoformat() if raw.published else None,
            title=raw.title,
            snippet=raw.snippet,
        )


def xe(value: str) -> str:
    """Escape ``&``, ``<`` and ``>`` so item text cannot close ``</item>`` or open a tag (LP §2.2)."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _prompts_root() -> Traversable:
    return files("stockmanager") / "prompts"


def load_extractor_prompt(root: Traversable | None = None, version: str | None = None) -> ExtractorPrompt:
    """Load ``extractor/<version>`` (default: the manifest's active version), verifying every file's sha256."""
    base = root if root is not None else _prompts_root()
    try:
        if version is None:
            manifest = yaml.safe_load((base / "manifest.yaml").read_text(encoding="utf-8"))
            version = str(manifest["active"]["extractor"])
        directory = base / "extractor" / version
        meta = yaml.safe_load((directory / "meta.yaml").read_text(encoding="utf-8"))
        texts: dict[str, bytes] = {}
        for name, expected in meta["files"].items():
            data = (directory / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise PromptIntegrityError(f"extractor/{version}/{name} does not match meta.yaml")
            texts[name] = data
        env = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined, autoescape=False)
        env.filters["xe"] = xe
        digest = hashlib.sha256(
            "".join(f"{n}:{hashlib.sha256(d).hexdigest()}\n" for n, d in sorted(texts.items())).encode()
        )
        return ExtractorPrompt(
            version=version,
            sha256=digest.hexdigest(),
            target_model=str(meta["target_model"]),
            system=texts["system.md"].decode("utf-8"),
            user_template=env.from_string(texts["user.md.j2"].decode("utf-8")),
            schema=json.loads(texts["output_schema.json"]),
        )
    except ExtractorUnavailable:
        raise
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError, jinja2.TemplateError) as e:
        raise PromptIntegrityError(f"cannot load extractor prompt: {type(e).__name__}") from e


# -- parsing -----------------------------------------------------------------------------------------------------


def parse_candidates(text: str) -> list[Candidate]:
    """Parse the model's JSON object. Raises ``ExtractFailure("extract_schema")`` for anything but ``{"candidates": [..]}``.

    More than five candidates fails the whole item rather than being truncated: an item that makes the model emit a
    flood is not one whose first five records deserve trust.
    """
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ExtractFailure("extract_schema") from e
    if not isinstance(doc, dict) or set(doc) != {"candidates"} or not isinstance(doc["candidates"], list):
        raise ExtractFailure("extract_schema")
    if len(doc["candidates"]) > MAX_CANDIDATES:
        raise ExtractFailure("extract_schema")
    try:
        return [Candidate.model_validate(c) for c in doc["candidates"]]
    except ValidationError as e:
        raise ExtractFailure("extract_schema") from e


class _Messages(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class _Client(Protocol):
    @property
    def messages(self) -> _Messages: ...


class AnthropicExtractor:
    """Calls ``llm.extractor_model`` with structured outputs: no tools, no web, thinking off, effort low."""

    def __init__(self, prompt: ExtractorPrompt, config: NewsConfig, client: _Client | None = None) -> None:
        self._prompt = prompt
        self._model = config.extractor_model
        self._max_tokens = config.extractor.max_tokens
        self._timeout = config.extractor.timeout_s
        if prompt.target_model != self._model:
            log.warning(
                "extractor prompt %s was tested on %s, configured model is %s",
                prompt.version,
                prompt.target_model,
                self._model,
            )
        # The SDK retries 408/409/429/5xx and timeouts itself.
        self._client: _Client = client or cast(
            _Client, anthropic.Anthropic(max_retries=config.extractor.max_retries, timeout=self._timeout)
        )

    def verify(self) -> None:
        """The prompt was verified when loaded; nothing further to check."""

    def extract(self, item: PreparedItem, ctx: ExtractContext) -> list[Candidate]:
        """One call for one item. Raises ``ExtractFailure`` with the §4.3 reason code."""
        user = self._prompt.render_user(item, ctx)
        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=self._prompt.system,
                messages=[{"role": "user", "content": user}],
                thinking={"type": "disabled"},
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": self._prompt.schema}},
                timeout=self._timeout,
            )
        except anthropic.APITimeoutError as e:
            raise ExtractFailure("extract_timeout", infra=True) from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise ExtractFailure("extract_infra", infra=True, auth=True) from e
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            raise ExtractFailure("extract_infra", infra=True) from e
        except Exception as e:  # missing or unresolvable credentials surface as TypeError / AnthropicError
            raise ExtractFailure("extract_infra", infra=True, auth=True) from e
        stop = str(resp.stop_reason)
        if stop == "refusal":
            raise ExtractFailure("extract_refusal")
        if stop == "max_tokens":
            raise ExtractFailure("extract_max_tokens")
        text = "".join(b.text for b in resp.content if b.type == "text")
        return parse_candidates(text)


class UnavailableExtractor:
    """Stands in when the extractor cannot be built (for example a prompt integrity failure)."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def verify(self) -> None:
        """Always raises."""
        raise ExtractorUnavailable(self._reason)

    def extract(self, item: PreparedItem, ctx: ExtractContext) -> list[Candidate]:
        """Always raises."""
        raise ExtractorUnavailable(self._reason)


def build_extractor(config: NewsConfig, client: _Client | None = None) -> Extractor:
    """The real extractor, or an ``UnavailableExtractor`` if the prompt fails its integrity check."""
    try:
        return AnthropicExtractor(load_extractor_prompt(), config, client)
    except ExtractorUnavailable as e:
        return UnavailableExtractor(str(e))


# -- the stage ---------------------------------------------------------------------------------------------------


class ExtractorHealth:
    """Extractor-down detector (news-sources §6.2).

    Down when any 401/403 occurred, or when at least ``MIN_ATTEMPTS_FOR_DOWN`` items were attempted and at least
    ``DOWN_FAILURE_RATE`` of the attempts ended in an infrastructure failure. Refusals, schema failures and
    ``max_tokens`` are item-level and never count.
    """

    def __init__(self) -> None:
        self.attempted = 0
        self.infra_failures = 0
        self.auth_failed = False

    def record(self, failure: ExtractFailure | None) -> None:
        """Count one finished attempt (``None`` = success)."""
        self.attempted += 1
        if failure is not None and failure.infra:
            self.infra_failures += 1
            self.auth_failed = self.auth_failed or failure.auth

    @property
    def down(self) -> bool:
        """True once the extractor should be treated as unavailable."""
        if self.auth_failed:
            return True
        return self.attempted >= MIN_ATTEMPTS_FOR_DOWN and self.infra_failures / self.attempted >= DOWN_FAILURE_RATE


@dataclass
class ExtractionOutcome:
    """Result of the extraction stage."""

    candidates: dict[str, list[Candidate]]  # by item_id, for items that were extracted
    rejections: list[Rejection]
    attempted: int
    infra_failures: int
    down: bool


def run_extraction(
    extractor: Extractor,
    items: Sequence[PreparedItem],
    ctx: ExtractContext,
    config: NewsConfig,
    monotonic: Callable[[], float] = time.monotonic,
) -> ExtractionOutcome:
    """One call per item on up to ``extractor.concurrency`` threads, with a stage deadline and the down detector."""
    health = ExtractorHealth()
    health_lock = threading.Lock()
    stop = threading.Event()  # set when the extractor is down or the stage is over: no new calls start
    candidates: dict[str, list[Candidate]] = {}
    rejections: list[Rejection] = []
    outcome_t = tuple[list[Candidate] | None, ExtractFailure | None]

    def work(item: PreparedItem) -> outcome_t:
        if stop.is_set():
            return None, None  # skipped: never attempted
        result: list[Candidate] | None = None
        failure: ExtractFailure | None = None
        try:
            result = extractor.extract(item, ctx)
        except ExtractFailure as f:
            failure = f
        except ExtractorUnavailable:
            failure = ExtractFailure("extract_infra", infra=True, auth=True)
        except Exception:  # a bug in one call must not take down the stage
            log.exception("unexpected extractor error")
            failure = ExtractFailure("extract_infra", infra=True)
        with health_lock:  # judged as results land, so a dead extractor stops further calls at once
            health.record(failure)
            if health.down:
                stop.set()
        return result, failure

    def collect(fut: Future[outcome_t]) -> None:
        item = futures[fut]
        result, failure = fut.result()
        if failure is not None:
            rejections.append(Rejection("extract", failure.reason, item.item_id, item.adapter))
        elif result is not None:
            candidates[item.item_id] = result

    deadline = monotonic() + config.extract_stage_deadline_s
    pool = ThreadPoolExecutor(max_workers=config.extractor.concurrency, thread_name_prefix="extract")
    futures: dict[Future[outcome_t], PreparedItem] = {pool.submit(work, item): item for item in items}
    pending = set(futures)
    try:
        while pending and not stop.is_set():
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            for fut in done:
                collect(fut)
    finally:
        stop.set()
        pool.shutdown(wait=False, cancel_futures=True)
    for fut in pending:
        if fut.done() and not fut.cancelled():
            collect(fut)
        elif not health.down:
            item = futures[fut]
            rejections.append(Rejection("extract", "extract_deadline", item.item_id, item.adapter))
    return ExtractionOutcome(candidates, rejections, health.attempted, health.infra_failures, health.down)
