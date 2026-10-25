"""Q1 shadow-run benchmark harness (v2 test plan §4, requirements R7/R10): is Strata fit to own the triage classifier?

Replays a frozen fetch-log corpus (``workspace/fetch-log/<cycle_id>.jsonl``, D8) through the same two gates the
pipeline applies (pre-filter, then confidence threshold plus citation) and measures:

* p50/p95 classification latency per item and p95 per batch of ``triage.max_items_per_run`` items;
* the false-positive rate and noise ratio against human labels (``labels.json``, see ``load_labels``);
* coverage: how much of the corpus is actually labeled. Metrics are computed on labeled items only, and a metric with
  no data is ``None``, never zero.

Definitions (labeled, classified items only; an item "escalates" if any held ticker clears the gates):

* ``fp_rate``     = FP / (FP + TN): share of ``noise`` items that escalate;
* ``noise_ratio`` = FP / (TP + FP): share of escalations that are ``noise``;
* ``recall``      = TP / (TP + FN): informational, not gated.

Thresholds come from ``TriageConfig`` (``benchmark_*``, ``confidence_threshold``), never from this module. Dedupe, the
daily cap and quiet hours are not replayed: they change when an escalation lands, not whether the model was right.

Fail closed: an empty corpus, a malformed row or label, a corpus that does not match ``expect_hash`` or an
unavailable classifier never yields a ``pass``. Everything but the latency figures is a pure function of the corpus,
labels, held tickers, classifier outputs and config, so a rerun against the same corpus hash reproduces it exactly.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import sys
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from stockmanager.config.model import Config
from stockmanager.config.triage import TriageConfig
from stockmanager.store import canonical_json
from stockmanager.store.fsutil import atomic_write_text
from stockmanager.triage.classifier import ClassifierUnavailable, TriageClassifier
from stockmanager.triage.models import TriageItem, has_citation
from stockmanager.triage.prefilter import prefilter

LABELS_SCHEMA_VERSION: Final = 1
Label = Literal["material", "noise"]
Status = Literal["pass", "fail", "incomplete"]
Timer = Callable[[], float]

_ROW_FIELDS: Final = ("item_hash", "source", "url", "raw_title", "raw_snippet", "fetched_at")
_EXIT_CODES: Final[dict[str, int]] = {"pass": 0, "fail": 1, "incomplete": 2}
EXIT_ERROR: Final = 3


class BenchmarkError(Exception):
    """The benchmark cannot produce a trustworthy result. The CLI exits non-zero and writes nothing."""


class EmptyCorpus(BenchmarkError):
    """No fetch-log rows: there is nothing to measure, so there is nothing to report (fail closed)."""


class CorpusError(BenchmarkError):
    """A fetch-log file is malformed, or the corpus does not match the pinned hash."""


class LabelsError(BenchmarkError):
    """The labels file is missing (when named) or malformed."""


# -- corpus -----------------------------------------------------------------------------------------------------------


class _Row(BaseModel):
    """One fetch-log row (R10): exactly the six captured fields.

    The capture (``news.pipeline._capture_fetch_log``) also writes ``cycle_id``; it is
    accepted here because it is already known from the filename (the D8 capture predates
    this loader and its rows must replay).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_hash: str
    source: str
    url: str
    raw_title: str
    raw_snippet: str
    fetched_at: str
    cycle_id: str | None = None


@dataclass(frozen=True)
class CorpusItem:
    """A fetch-log row with the ``cycle_id`` of the file it came from."""

    cycle_id: str
    item: TriageItem

    @property
    def key(self) -> tuple[str, str]:
        """The label key: ``(cycle_id, item_hash)``."""
        return (self.cycle_id, self.item.item_hash)


@dataclass(frozen=True)
class Corpus:
    """A frozen corpus: items in (cycle_id, file order) order and the hash that identifies exactly this content."""

    items: tuple[CorpusItem, ...]
    hash: str

    @property
    def cycles(self) -> int:
        """Number of distinct cycles (files)."""
        return len({c.cycle_id for c in self.items})


def _canonical_line(cycle_id: str, row: _Row) -> str:
    data = {"cycle_id": cycle_id, **{f: getattr(row, f) for f in _ROW_FIELDS}}
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def load_corpus(directory: Path) -> Corpus:
    """Read every ``*.jsonl`` under ``directory`` (cycle id = file stem, sorted) into a hashed ``Corpus``.

    The hash is SHA-256 over one canonical line per row (cycle id + the six captured fields), so it ignores file
    mtimes, blank lines and key order but changes with any row's content, any added or removed row, or a reorder.
    Raises ``EmptyCorpus`` when there are no rows, ``CorpusError`` on a malformed or duplicated row.
    """
    files = sorted(directory.glob("*.jsonl")) if directory.is_dir() else []
    digest = hashlib.sha256()
    items: list[CorpusItem] = []
    seen: set[tuple[str, str]] = set()
    for path in files:
        cycle_id = path.stem
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            where = f"{path.name}:{lineno}"
            try:
                row = _Row.model_validate_json(line)
                fetched_at = datetime.fromisoformat(row.fetched_at)
            except (ValidationError, ValueError) as exc:
                raise CorpusError(f"{where}: not a valid fetch-log row ({type(exc).__name__})") from exc
            if fetched_at.tzinfo is None:
                raise CorpusError(f"{where}: fetched_at must carry a UTC offset")
            if (cycle_id, row.item_hash) in seen:
                raise CorpusError(f"{where}: duplicate item_hash {row.item_hash!r} in cycle {cycle_id!r}")
            seen.add((cycle_id, row.item_hash))
            digest.update(_canonical_line(cycle_id, row).encode("utf-8") + b"\n")
            item = TriageItem(row.item_hash, row.url, row.raw_title, row.raw_snippet, fetched_at, row.source)
            items.append(CorpusItem(cycle_id, item))
    if not items:
        raise EmptyCorpus(f"no fetch-log rows under {directory}: nothing to benchmark")
    return Corpus(tuple(items), f"sha256:{digest.hexdigest()}")


# -- labels -----------------------------------------------------------------------------------------------------------


class _LabelsFile(BaseModel):
    """``labels.json``: ``{"schema_version": 1, "labels": {"<cycle_id>": {"<item_hash>": "material" | "noise"}}}``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    labels: dict[str, dict[str, Label]]


@dataclass(frozen=True)
class Labels:
    """Human labels keyed by ``(cycle_id, item_hash)``. Empty is valid: nothing is labeled until someone labels it."""

    by_key: dict[tuple[str, str], Label]
    hash: str


def _labels_hash(nested: dict[str, dict[str, Label]]) -> str:
    text = json.dumps(nested, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def load_labels(path: Path | None) -> Labels:
    """Load the labels file. ``None`` means no labels yet (valid); a named file that is missing or malformed is not."""
    if path is None:
        return Labels({}, _labels_hash({}))
    try:
        doc = _LabelsFile.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise LabelsError(f"{path}: {type(exc).__name__}: cannot load labels") from exc
    flat: dict[tuple[str, str], Label] = {(c, h): v for c, items in doc.labels.items() for h, v in items.items()}
    return Labels(flat, _labels_hash(doc.labels))


# -- measurement ------------------------------------------------------------------------------------------------------


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile (deterministic, no interpolation); ``None`` for no data."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(pct / 100 * len(ordered)), 1) - 1]


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


@dataclass(frozen=True)
class Thresholds:
    """The pass criteria in force, copied from ``TriageConfig`` so a stored result says what it was judged against."""

    confidence_threshold: float
    item_p95_seconds: float
    batch_p95_seconds: float
    max_fp_rate: float
    max_noise_ratio: float
    batch_size: int

    @classmethod
    def from_config(cls, cfg: TriageConfig) -> Thresholds:
        """Read the criteria from the ``triage`` config section."""
        return cls(
            cfg.confidence_threshold,
            cfg.benchmark_item_p95_seconds,
            cfg.benchmark_batch_p95_seconds,
            cfg.benchmark_max_fp_rate,
            cfg.benchmark_max_noise_ratio,
            cfg.max_items_per_run,
        )


@dataclass(frozen=True)
class Check:
    """One pass criterion: ``ok`` is ``None`` when ``value`` could not be measured (never a silent pass)."""

    name: str
    value: float | None
    limit: float
    ok: bool | None


def _check(name: str, value: float | None, limit: float) -> Check:
    return Check(name, value, limit, None if value is None else value <= limit)


@dataclass(frozen=True)
class BenchmarkResult:
    """Everything one run measured, plus the identity (corpus/labels hash, classifier, held) it measured it on."""

    corpus_hash: str
    labels_hash: str
    classifier: str
    held: tuple[str, ...]
    cycles: int
    items: int
    first_fetched_at: str
    last_fetched_at: str
    gated: int  # items that passed the pre-filter and reached the classifier
    classifier_calls: int
    unavailable: int  # items the classifier could not answer; excluded from every metric
    escalated: int
    labeled: int  # corpus items that carry a label
    labeled_decided: int  # ... and were decided (metrics are computed on these)
    orphan_labels: int  # labels naming no corpus item
    coverage: float
    tp: int
    fp: int
    fn: int
    tn: int
    fp_rate: float | None
    noise_ratio: float | None
    recall: float | None
    latency_p50: float | None
    latency_p95: float | None
    latency_max: float | None
    batches: int
    batch_p95: float | None
    thresholds: Thresholds
    checks: tuple[Check, ...]
    status: Status

    def to_json(self) -> str:
        """Canonical JSON (indent 2, trailing newline) of the whole result."""
        return canonical_json(asdict(self))


def run_benchmark(
    corpus: Corpus,
    labels: Labels,
    classifier: TriageClassifier,
    held: Collection[str],
    cfg: TriageConfig,
    *,
    classifier_name: str = "",
    expect_hash: str | None = None,
    timer: Timer = time.perf_counter,
) -> BenchmarkResult:
    """Replay ``corpus`` through ``classifier`` and score it against ``labels`` and the thresholds in ``cfg``.

    ``expect_hash`` pins the corpus: any other content raises ``CorpusError``. ``timer`` is the latency seam.
    """
    if expect_hash is not None and expect_hash != corpus.hash:
        raise CorpusError(f"corpus hash {corpus.hash} does not match the pinned {expect_hash}")
    held_set = frozenset(held)
    if not held_set:
        raise BenchmarkError("no held tickers: the pre-filter would gate out every item")
    limits = Thresholds.from_config(cfg)

    latencies: list[float] = []
    unavailable = escalated = gated = 0
    outcome: dict[tuple[str, str], bool] = {}  # decided items: key -> escalated
    for ci in corpus.items:
        gate = prefilter(ci.item, held_set)
        if not gate.passed:
            # Pre-filtered out: the classifier never ran, so this item is not a model decision
            # and does not enter tp/fp/fn/tn (definitions say: labeled, classified items only).
            continue
        gated += 1
        hit = down = False
        for ticker in gate.tickers:
            started = timer()
            try:
                verdict = classifier.classify(ci.item, ticker, gate.kinds)
            except ClassifierUnavailable:
                down = True
                break
            latencies.append(timer() - started)
            hit = hit or (verdict.confidence >= limits.confidence_threshold and has_citation(ci.item.url))
        if down:
            unavailable += 1
            continue
        outcome[ci.key] = hit
        escalated += hit

    tp = fp = fn = tn = 0
    labeled = 0
    for ci in corpus.items:
        label = labels.by_key.get(ci.key)
        if label is None:
            continue
        labeled += 1
        if ci.key not in outcome:
            continue
        esc = outcome[ci.key]
        if label == "material":
            tp, fn = tp + esc, fn + (not esc)
        else:
            fp, tn = fp + esc, tn + (not esc)
    corpus_keys = {ci.key for ci in corpus.items}
    orphans = sum(1 for k in labels.by_key if k not in corpus_keys)

    batches = [sum(latencies[i : i + limits.batch_size]) for i in range(0, len(latencies), limits.batch_size)]
    fp_rate, noise_ratio = _ratio(fp, fp + tn), _ratio(fp, tp + fp)
    p95, batch_p95 = percentile(latencies, 95), percentile(batches, 95)
    checks = (
        _check("item_p95_seconds", p95, limits.item_p95_seconds),
        _check("batch_p95_seconds", batch_p95, limits.batch_p95_seconds),
        _check("fp_rate", fp_rate, limits.max_fp_rate),
        _check("noise_ratio", noise_ratio, limits.max_noise_ratio),
    )
    status: Status
    if any(c.ok is False for c in checks):
        status = "fail"
    elif unavailable or any(c.ok is None for c in checks):
        status = "incomplete"
    else:
        status = "pass"
    stamps = [ci.item.fetched_at for ci in corpus.items]
    return BenchmarkResult(
        corpus_hash=corpus.hash,
        labels_hash=labels.hash,
        classifier=classifier_name,
        held=tuple(sorted(held_set)),
        cycles=corpus.cycles,
        items=len(corpus.items),
        first_fetched_at=min(stamps).isoformat(),
        last_fetched_at=max(stamps).isoformat(),
        gated=gated,
        classifier_calls=len(latencies),
        unavailable=unavailable,
        escalated=escalated,
        labeled=labeled,
        labeled_decided=tp + fp + fn + tn,
        orphan_labels=orphans,
        coverage=labeled / len(corpus.items),
        tp=tp,
        fp=fp,
        fn=fn,
        tn=tn,
        fp_rate=fp_rate,
        noise_ratio=noise_ratio,
        recall=_ratio(tp, tp + fn),
        latency_p50=percentile(latencies, 50),
        latency_p95=p95,
        latency_max=max(latencies) if latencies else None,
        batches=len(batches),
        batch_p95=batch_p95,
        thresholds=limits,
        checks=checks,
        status=status,
    )


# -- report -----------------------------------------------------------------------------------------------------------

_PENDING: Final = "_pending_"


def _num(value: float | None, unit: str = "", digits: int = 4) -> str:
    return _PENDING if value is None else f"{value:.{digits}f}{unit}"


def _pct(value: float | None) -> str:
    return _PENDING if value is None else f"{value * 100:.1f}%"


def render_report(cfg: TriageConfig, result: BenchmarkResult | None = None, rerun_command: str | None = None) -> str:
    """Markdown for ``docs/baseline-benchmark-v2.md``. With ``result=None`` it is the empty template (all values pending).

    A value appears only if it was measured; anything else renders ``_pending_``. Nothing here is computed beyond the
    result, so the same result renders the same text.
    """
    r = result
    limits = r.thresholds if r else Thresholds.from_config(cfg)
    checks = {c.name: c for c in r.checks} if r else {}

    def verdict(name: str) -> str:
        ok = checks[name].ok if name in checks else None
        return _PENDING if ok is None else ("pass" if ok else "**FAIL**")

    lines = [
        "# Baseline benchmark v2 — Q1 triage classifier",
        "",
        "Gate for `triage.benchmark_passed` (test plan §4): the shadow-run corpus, its labels and the numbers measured on it.",
        "Generated by `stockmanager.triage.benchmark`; do not edit by hand. A value is filled only when it was measured.",
        "",
        f"**Status:** {r.status if r else _PENDING}",
        "",
        "## Corpus",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Corpus hash | `{r.corpus_hash}` |" if r else f"| Corpus hash | {_PENDING} |",
        f"| Labels hash | `{r.labels_hash}` |" if r else f"| Labels hash | {_PENDING} |",
        f"| Classifier | {(r.classifier or _PENDING) if r else _PENDING} |",
        f"| Held tickers | {', '.join(r.held) if r else _PENDING} |",
        f"| Cycles | {r.cycles if r else _PENDING} |",
        f"| Items | {r.items if r else _PENDING} |",
        f"| First fetched | {r.first_fetched_at if r else _PENDING} |",
        f"| Last fetched | {r.last_fetched_at if r else _PENDING} |",
        "",
        "## Label coverage",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Labeled items | {r.labeled if r else _PENDING} |",
        f"| Coverage (labeled / items) | {_pct(r.coverage) if r else _PENDING} |",
        f"| Labeled and decided | {r.labeled_decided if r else _PENDING} |",
        f"| Labels naming no corpus item | {r.orphan_labels if r else _PENDING} |",
        f"| TP / FP / FN / TN | {f'{r.tp} / {r.fp} / {r.fn} / {r.tn}' if r else _PENDING} |",
        f"| Items gated to the classifier | {r.gated if r else _PENDING} |",
        f"| Classifier calls | {r.classifier_calls if r else _PENDING} |",
        f"| Items the classifier could not answer | {r.unavailable if r else _PENDING} |",
        f"| Escalated | {r.escalated if r else _PENDING} |",
        "",
        "Metrics below use labeled, decided items only. Unlabeled items contribute to latency, not to quality.",
        "",
        "## Results",
        "",
        f"Confidence threshold: {limits.confidence_threshold}; batch size: {limits.batch_size} items.",
        "",
        "| Criterion | Measured | Limit | Verdict |",
        "|---|---|---|---|",
        f"| p95 latency per item | {_num(r.latency_p95 if r else None, ' s')} | ≤ {limits.item_p95_seconds} s | {verdict('item_p95_seconds')} |",
        f"| p95 latency per batch | {_num(r.batch_p95 if r else None, ' s')} | ≤ {limits.batch_p95_seconds} s | {verdict('batch_p95_seconds')} |",
        f"| False-positive rate | {_pct(r.fp_rate if r else None)} | ≤ {limits.max_fp_rate * 100:.1f}% | {verdict('fp_rate')} |",
        f"| Noise ratio | {_pct(r.noise_ratio if r else None)} | ≤ {limits.max_noise_ratio * 100:.1f}% | {verdict('noise_ratio')} |",
        "",
        (
            f"Informational: p50 latency {_num(r.latency_p50 if r else None, ' s')}, "
            f"max latency {_num(r.latency_max if r else None, ' s')}, "
            f"recall {_pct(r.recall if r else None)}."
        ),
        "",
        "## Definitions",
        "",
        "- FP rate = FP / (FP + TN): share of `noise` items that escalate. Noise ratio = FP / (TP + FP): share of escalations that are `noise`.",
        "- An item escalates if any held ticker passes the pre-filter, the classifier's confidence is ≥ the threshold, and the item has a citable URL.",
        "- Percentiles are nearest-rank. Dedupe, the daily cap and quiet hours are not replayed.",
        "- Status: `pass` only if every criterion was measured and met and the classifier answered every gated item; `fail` if any measured criterion misses; otherwise `incomplete`.",
        "- Latency is wall-clock and varies run to run; every other figure is reproducible from the corpus hash.",
        "",
        "## Rerun",
        "",
        "```",
        rerun_command or _PENDING,
        "```",
        "",
    ]
    return "\n".join(lines)


# -- CLI --------------------------------------------------------------------------------------------------------------


def _load_classifier(spec: str) -> TriageClassifier:
    """``module:factory`` -> ``factory()``. The operator names the model under test; there is no default."""
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise BenchmarkError(f"--classifier must be 'module:factory', got {spec!r}")
    try:
        factory = getattr(importlib.import_module(module_name), attr)
        classifier: TriageClassifier = factory()
    except (ImportError, AttributeError, TypeError) as exc:
        raise BenchmarkError(f"cannot build classifier {spec!r}: {type(exc).__name__}: {exc}") from exc
    return classifier


def _load_triage_config(path: Path) -> TriageConfig:
    if not path.exists():
        return TriageConfig()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Config.model_validate(data).triage
    except (yaml.YAMLError, ValidationError, ValueError) as exc:
        raise BenchmarkError(f"{path}: invalid config ({type(exc).__name__})") from exc


def _rerun_command(args: argparse.Namespace, corpus_hash: str) -> str:
    parts = ["python scripts/benchmark.py", f"--workspace {args.workspace}", f"--corpus {args.corpus}"]
    if args.labels:
        parts.append(f"--labels {args.labels}")
    parts += [f"--held {','.join(sorted(_split_held(args.held)))}", f"--classifier {args.classifier}"]
    parts.append(f"--expect-hash {corpus_hash}")
    if args.report:
        parts.append(f"--report {args.report}")
    return " \\\n  ".join(parts)


def _split_held(raw: str) -> set[str]:
    return {t.strip().upper() for t in raw.split(",") if t.strip()}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Q1 shadow-run benchmark for the triage classifier (test plan §4).")
    p.add_argument("--workspace", type=Path, default=Path("workspace"), help="holds config.yaml (triage thresholds)")
    p.add_argument("--corpus", type=Path, help="fetch-log directory (default: <workspace>/fetch-log)")
    p.add_argument("--labels", type=Path, help="labels.json; omit for an unlabeled run")
    p.add_argument("--held", default="", help="comma-separated held tickers the pre-filter gates on")
    p.add_argument("--classifier", default="", help="module:factory building the TriageClassifier under test")
    p.add_argument("--classifier-name", default="", help="name recorded in the report (default: --classifier)")
    p.add_argument("--expect-hash", help="pin the corpus: fail unless its hash matches")
    p.add_argument("--report", type=Path, help="write the markdown report here (docs/baseline-benchmark-v2.md)")
    p.add_argument("--json", type=Path, help="write the full result as canonical JSON here")
    p.add_argument("--template", action="store_true", help="write the empty report template and exit; measures nothing")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Run the benchmark. Exit 0 pass, 1 fail, 2 incomplete, 3 error (nothing written on error)."""
    args = _parser().parse_args(argv)
    args.corpus = args.corpus or args.workspace / "fetch-log"
    try:
        cfg = _load_triage_config(args.workspace / "config.yaml")
        if args.template:
            if not args.report:
                raise BenchmarkError("--template needs --report")
            atomic_write_text(args.report, render_report(cfg))
            return 0
        classifier = _load_classifier(args.classifier)
        corpus = load_corpus(args.corpus)
        result = run_benchmark(
            corpus,
            load_labels(args.labels),
            classifier,
            _split_held(args.held),
            cfg,
            classifier_name=args.classifier_name or args.classifier,
            expect_hash=args.expect_hash,
        )
    except BenchmarkError as exc:
        print(f"benchmark: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        atomic_write_text(args.json, result.to_json())
    if args.report:
        atomic_write_text(args.report, render_report(cfg, result, _rerun_command(args, corpus.hash)))
    print(f"benchmark: {result.status} (corpus {result.corpus_hash}, coverage {result.coverage * 100:.1f}%)")
    return _EXIT_CODES[result.status]


if __name__ == "__main__":
    raise SystemExit(main())
