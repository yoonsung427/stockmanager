"""Q1 benchmark rerun (v2 test plan §4, R14): the frozen corpus through the real LLM classifier.

Skipped by default (``-m 'not benchmark'``). Run explicitly, against a live endpoint::

    Q1_CORPUS_HASH=sha256:<hash> Q1_HELD=AAPL,NVDA TRIAGE_API_KEY=... \\
        .venv/bin/pytest tests/benchmark_q1.py -m benchmark -s

Reads ``<workspace>/fetch-log/*.jsonl``, ``<workspace>/labels.json`` (optional: absent means an unlabeled run, which can
only be ``incomplete``) and ``<workspace>/config.yaml`` (triage thresholds and endpoint). The workspace is
``$STOCKMANAGER_WORKSPACE`` (default ``workspace``). ``Q1_CORPUS_HASH`` is required: an unpinned run proves nothing.
Fail closed like ``triage.benchmark``: empty corpus, hash mismatch, malformed input or any status but ``pass`` fails.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from stockmanager.config.triage import TriageConfig
from stockmanager.triage.benchmark import (
    BenchmarkResult,
    CorpusError,
    EmptyCorpus,
    _load_triage_config,
    load_corpus,
    load_labels,
    run_benchmark,
)
from stockmanager.triage.classifier import TriageClassifier
from stockmanager.triage.llm_classifier import LLMClassifier

pytestmark = pytest.mark.benchmark


def run_q1(
    workspace: Path, expect_hash: str | None, held: str, classifier: TriageClassifier, cfg: TriageConfig
) -> BenchmarkResult:
    """Run the harness on the workspace corpus. Raises ``BenchmarkError`` subclasses; never defaults a missing pin."""
    if not expect_hash:
        raise CorpusError("Q1_CORPUS_HASH is not set: refusing an unpinned run")
    corpus = load_corpus(workspace / "fetch-log")
    labels_path = workspace / "labels.json"
    labels = load_labels(labels_path if labels_path.exists() else None)
    tickers = {t.strip().upper() for t in held.split(",") if t.strip()}
    return run_benchmark(
        corpus, labels, classifier, tickers, cfg, classifier_name=f"llm:{cfg.classifier_model}", expect_hash=expect_hash
    )


def print_result(result: BenchmarkResult) -> None:
    """Print the numbers, each against its limit, and the corpus hash."""
    print(f"\ncorpus hash : {result.corpus_hash}")
    print(f"labels hash : {result.labels_hash}")
    print(
        f"window      : {result.first_fetched_at} .. {result.last_fetched_at} ({result.cycles} cycles, {result.items} items)"
    )
    print(f"coverage    : {result.coverage * 100:.1f}% ({result.labeled} labeled, {result.labeled_decided} decided)")
    print(f"latency     : p50 {result.latency_p50} s, p95 {result.latency_p95} s, batch p95 {result.batch_p95} s")
    print(f"unavailable : {result.unavailable}")
    for c in result.checks:
        print(f"{c.name:18}: {c.value} (limit {c.limit}) ok={c.ok}")
    print(f"status      : {result.status}")


def test_q1_benchmark_rerun() -> None:
    """The real rerun. Needs the corpus, a pin, held tickers and a reachable endpoint."""
    workspace = Path(os.environ.get("STOCKMANAGER_WORKSPACE", "workspace"))
    cfg = _load_triage_config(workspace / "config.yaml")
    classifier = LLMClassifier.from_config(cfg)
    try:
        result = run_q1(workspace, os.environ.get("Q1_CORPUS_HASH"), os.environ.get("Q1_HELD", ""), classifier, cfg)
    finally:
        classifier.close()
    print_result(result)
    assert result.status == "pass"


def test_unpinned_run_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="unpinned"):
        run_q1(tmp_path, None, "AAPL", LLMClassifier("", "", ""), TriageConfig())


def test_empty_corpus_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(EmptyCorpus):
        run_q1(tmp_path, "sha256:x", "AAPL", LLMClassifier("", "", ""), TriageConfig())


def test_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    log = tmp_path / "fetch-log"
    log.mkdir()
    row = '{"item_hash":"a","source":"s","url":"https://x.example/a","raw_title":"AAPL","raw_snippet":"","fetched_at":"2026-10-25T06:30:00-07:00"}'
    (log / "c1.jsonl").write_text(row + "\n", encoding="utf-8")
    with pytest.raises(CorpusError, match="does not match"):
        run_q1(tmp_path, "sha256:wrong", "AAPL", LLMClassifier("", "", ""), TriageConfig())
