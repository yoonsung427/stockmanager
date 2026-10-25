"""Q1 shadow-run benchmark harness (v2 test plan §4): corpus hash, determinism, thresholds, coverage, fail-closed."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from stockmanager.config.triage import TriageConfig
from stockmanager.triage.benchmark import (
    BenchmarkError,
    BenchmarkResult,
    CorpusError,
    EmptyCorpus,
    LabelsError,
    load_corpus,
    load_labels,
    main,
    percentile,
    render_report,
    run_benchmark,
)
from stockmanager.triage.classifier import ClassifierUnavailable
from stockmanager.triage.models import Classification, EventKind, TriageItem

HELD = ("AAPL", "NVDA")
CYCLE_A, CYCLE_B = "2026-10-25-weekly", "2026-10-26-weekly"


def _row(item_hash: str, title: str, url: str = "https://news.example/story") -> dict[str, str]:
    return {
        "item_hash": item_hash,
        "source": "example",
        "url": url,
        "raw_title": title,
        "raw_snippet": "",
        "fetched_at": "2026-10-25T06:30:00-07:00",
    }


ROWS_A = [
    _row("a1", "NVDA beats estimates on quarterly earnings"),
    _row("a2", "NVDA upgraded by analyst"),
    _row("a3", "Weather report for Tuesday"),
]
ROWS_B = [_row("b1", "AAPL announces share repurchase")]


def _write(directory: Path, cycles: Mapping[str, Sequence[dict[str, str]]], blank_lines: bool = False) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for cycle, rows in cycles.items():
        sep = "\n\n" if blank_lines else "\n"
        (directory / f"{cycle}.jsonl").write_text(sep.join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return directory


def _labels_file(path: Path, labels: Mapping[str, Mapping[str, str]]) -> Path:
    path.write_text(json.dumps({"schema_version": 1, "labels": labels}), encoding="utf-8")
    return path


class _Model:
    """Scripted classifier on a fake clock: every call advances time by ``cost`` seconds."""

    def __init__(self, confidence: Mapping[str, float], cost: float = 2.0, down: bool = False) -> None:
        self.confidence = confidence
        self.cost = cost
        self.down = down
        self.now = 0.0

    def timer(self) -> float:
        return self.now

    def classify(self, item: TriageItem, ticker: str, hinted: Sequence[EventKind]) -> Classification:
        if self.down:
            raise ClassifierUnavailable("down")
        self.now += self.cost
        return Classification(EventKind.EARNINGS, self.confidence.get(item.item_hash, 0.0))


CONFIDENCE = {"a1": 0.9, "a2": 0.5, "b1": 0.95}  # a1, b1 clear the 0.85 default threshold; a2 does not


def _run(
    tmp_path: Path, labels: Path | None, model: _Model | None = None, cfg: TriageConfig | None = None
) -> BenchmarkResult:
    corpus = load_corpus(_write(tmp_path / "fetch-log", {CYCLE_A: ROWS_A, CYCLE_B: ROWS_B}))
    m = model or _Model(CONFIDENCE)
    return run_benchmark(corpus, load_labels(labels), m, HELD, cfg or TriageConfig(), timer=m.timer)


ALL_NOISE_B: dict[str, dict[str, str]] = {
    CYCLE_A: {"a1": "material", "a2": "noise", "a3": "noise"},
    CYCLE_B: {"b1": "noise"},
}


# -- corpus hash ------------------------------------------------------------------------------------------------------


def test_corpus_hash_is_stable_across_directories_and_blank_lines(tmp_path: Path) -> None:
    one = load_corpus(_write(tmp_path / "one", {CYCLE_A: ROWS_A, CYCLE_B: ROWS_B}))
    two = load_corpus(_write(tmp_path / "two", {CYCLE_B: ROWS_B, CYCLE_A: ROWS_A}, blank_lines=True))
    assert one.hash == two.hash
    assert one.hash.startswith("sha256:") and len(one.hash) == len("sha256:") + 64
    assert (one.cycles, len(one.items)) == (2, 4)


def test_corpus_hash_changes_with_content(tmp_path: Path) -> None:
    base = load_corpus(_write(tmp_path / "base", {CYCLE_A: ROWS_A})).hash
    edited = [_row("a1", "NVDA beats estimates on quarterly earnings!"), *ROWS_A[1:]]
    assert load_corpus(_write(tmp_path / "edited", {CYCLE_A: edited})).hash != base
    assert load_corpus(_write(tmp_path / "fewer", {CYCLE_A: ROWS_A[:2]})).hash != base
    assert load_corpus(_write(tmp_path / "renamed", {CYCLE_B: ROWS_A})).hash != base  # cycle id is part of identity


def test_malformed_and_duplicate_rows_fail_closed(tmp_path: Path) -> None:
    bad = _write(tmp_path / "bad", {CYCLE_A: ROWS_A})
    (bad / f"{CYCLE_A}.jsonl").write_text('{"item_hash": "x"}\n', encoding="utf-8")
    with pytest.raises(CorpusError):
        load_corpus(bad)
    naive = _write(tmp_path / "naive", {CYCLE_A: [{**ROWS_A[0], "fetched_at": "2026-10-25T06:30:00"}]})
    with pytest.raises(CorpusError):
        load_corpus(naive)
    with pytest.raises(CorpusError):
        load_corpus(_write(tmp_path / "dup", {CYCLE_A: [ROWS_A[0], ROWS_A[0]]}))


# -- fail closed on empty ---------------------------------------------------------------------------------------------


def test_empty_or_missing_corpus_raises(tmp_path: Path) -> None:
    with pytest.raises(EmptyCorpus):
        load_corpus(tmp_path / "missing")
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / f"{CYCLE_A}.jsonl").write_text("\n", encoding="utf-8")
    with pytest.raises(EmptyCorpus):
        load_corpus(tmp_path / "empty")


def test_cli_empty_corpus_exits_3_and_writes_nothing(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    code = main(
        [
            "--workspace", str(tmp_path),
            "--held", "NVDA",
            "--classifier", "stockmanager.triage.classifier:UnavailableClassifier",
            "--report", str(report),
            "--json", str(tmp_path / "result.json"),
        ]
    )  # fmt: skip
    assert code == 3
    assert not report.exists() and not (tmp_path / "result.json").exists()


def test_cli_without_classifier_or_held_fails_closed(tmp_path: Path) -> None:
    _write(tmp_path / "fetch-log", {CYCLE_A: ROWS_A})
    assert main(["--workspace", str(tmp_path), "--held", "NVDA"]) == 3
    assert main(["--workspace", str(tmp_path), "--classifier", "stockmanager.triage.classifier:UnavailableClassifier"]) == 3


def test_classifier_down_is_incomplete_never_pass(tmp_path: Path) -> None:
    labels = _labels_file(tmp_path / "labels.json", ALL_NOISE_B)
    result = _run(tmp_path, labels, _Model(CONFIDENCE, down=True))
    assert result.status == "incomplete"
    assert result.unavailable == 3 and result.classifier_calls == 0
    assert result.latency_p95 is None and result.fp_rate is None


def test_cli_unavailable_classifier_writes_incomplete_report(tmp_path: Path) -> None:
    _write(tmp_path / "fetch-log", {CYCLE_A: ROWS_A})
    report = tmp_path / "report.md"
    args = ["--workspace", str(tmp_path), "--held", "NVDA", "--report", str(report)]
    assert main([*args, "--classifier", "stockmanager.triage.classifier:UnavailableClassifier"]) == 2
    text = report.read_text(encoding="utf-8")
    assert "**Status:** incomplete" in text and "--expect-hash sha256:" in text


# -- determinism ------------------------------------------------------------------------------------------------------


def test_same_corpus_and_hash_give_identical_results(tmp_path: Path) -> None:
    labels = _labels_file(tmp_path / "labels.json", ALL_NOISE_B)
    corpus = load_corpus(_write(tmp_path / "fetch-log", {CYCLE_A: ROWS_A, CYCLE_B: ROWS_B}))
    results = []
    for _ in range(2):
        m = _Model(CONFIDENCE)
        results.append(
            run_benchmark(corpus, load_labels(labels), m, HELD, TriageConfig(), timer=m.timer, expect_hash=corpus.hash)
        )
    assert results[0] == results[1]
    assert results[0].to_json() == results[1].to_json()
    assert render_report(TriageConfig(), results[0], "cmd") == render_report(TriageConfig(), results[1], "cmd")


def test_pinned_hash_mismatch_raises(tmp_path: Path) -> None:
    corpus = load_corpus(_write(tmp_path / "fetch-log", {CYCLE_A: ROWS_A}))
    with pytest.raises(CorpusError):
        run_benchmark(corpus, load_labels(None), _Model(CONFIDENCE), HELD, TriageConfig(), expect_hash="sha256:" + "0" * 64)


def test_no_held_tickers_raises(tmp_path: Path) -> None:
    corpus = load_corpus(_write(tmp_path / "fetch-log", {CYCLE_A: ROWS_A}))
    with pytest.raises(BenchmarkError):
        run_benchmark(corpus, load_labels(None), _Model(CONFIDENCE), (), TriageConfig())


# -- measurement and thresholds ---------------------------------------------------------------------------------------


def test_metrics_on_fully_labeled_corpus(tmp_path: Path) -> None:
    r = _run(tmp_path, _labels_file(tmp_path / "labels.json", ALL_NOISE_B))
    assert (r.items, r.gated, r.classifier_calls, r.escalated) == (4, 3, 3, 2)
    # a3 fails the pre-filter: never classified, so it is not a model decision and does not
    # enter the quality counts (definitions: labeled, classified items only).
    assert (r.tp, r.fp, r.fn, r.tn) == (1, 1, 0, 1)
    assert r.fp_rate == 0.5 and r.noise_ratio == 0.5 and r.recall == 1.0
    assert (r.latency_p50, r.latency_p95, r.latency_max) == (2.0, 2.0, 2.0)
    assert (r.batches, r.batch_p95) == (1, 6.0)
    assert r.coverage == 1.0 and r.status == "fail"  # 50% FP and 50% noise exceed the 10% / 20% defaults


def test_all_criteria_met_passes(tmp_path: Path) -> None:
    labels: dict[str, dict[str, str]] = {CYCLE_A: {"a1": "material", "a2": "noise"}, CYCLE_B: {"b1": "material"}}
    r = _run(tmp_path, _labels_file(tmp_path / "labels.json", labels))
    assert (r.fp_rate, r.noise_ratio) == (0.0, 0.0)
    assert r.status == "pass" and all(c.ok for c in r.checks)


def test_thresholds_come_from_config(tmp_path: Path) -> None:
    labels = _labels_file(tmp_path / "labels.json", {CYCLE_A: {"a1": "material", "a2": "noise"}, CYCLE_B: {"b1": "material"}})
    assert _run(tmp_path, labels).status == "pass"
    slow = _run(tmp_path, labels, cfg=TriageConfig(benchmark_item_p95_seconds=1.0))  # item latency is 2 s
    assert slow.status == "fail" and slow.thresholds.item_p95_seconds == 1.0
    assert [c.name for c in slow.checks if c.ok is False] == ["item_p95_seconds"]
    batch = _run(tmp_path, labels, cfg=TriageConfig(benchmark_batch_p95_seconds=5.0))  # batch of 3 calls is 6 s
    assert [c.name for c in batch.checks if c.ok is False] == ["batch_p95_seconds"]
    strict = _run(tmp_path, labels, cfg=TriageConfig(confidence_threshold=0.99))  # nothing clears it: no TP, no FP
    assert strict.escalated == 0 and strict.noise_ratio is None and strict.status == "incomplete"
    lenient = _run(tmp_path, _labels_file(tmp_path / "l2.json", ALL_NOISE_B), cfg=TriageConfig(benchmark_max_fp_rate=0.5, benchmark_max_noise_ratio=0.5))
    assert lenient.status == "pass"


def test_batches_follow_max_items_per_run(tmp_path: Path) -> None:
    r = _run(tmp_path, None, cfg=TriageConfig(max_items_per_run=2))
    assert (r.classifier_calls, r.batches, r.batch_p95) == (3, 2, 4.0)


def test_uncitable_item_cannot_escalate(tmp_path: Path) -> None:
    rows = [_row("a1", "NVDA beats estimates on quarterly earnings", url="not a url")]
    corpus = load_corpus(_write(tmp_path / "fetch-log", {CYCLE_A: rows}))
    m = _Model({"a1": 1.0})
    assert run_benchmark(corpus, load_labels(None), m, HELD, TriageConfig(), timer=m.timer).escalated == 0


def test_percentile_is_nearest_rank() -> None:
    assert percentile([], 95) is None
    assert percentile([5.0], 95) == 5.0
    assert percentile([float(i) for i in range(1, 101)], 95) == 95.0
    assert percentile([3.0, 1.0, 2.0], 50) == 2.0


# -- coverage on empty / partial labels -------------------------------------------------------------------------------


def test_no_labels_reports_zero_coverage_and_no_quality_numbers(tmp_path: Path) -> None:
    r = _run(tmp_path, None)
    assert (r.labeled, r.labeled_decided, r.coverage) == (0, 0, 0.0)
    assert (r.fp_rate, r.noise_ratio, r.recall) == (None, None, None)
    assert r.latency_p95 == 2.0  # latency needs no labels
    assert r.status == "incomplete"
    assert [c.name for c in r.checks if c.ok is None] == ["fp_rate", "noise_ratio"]


def test_partial_labels_report_coverage_and_use_labeled_items_only(tmp_path: Path) -> None:
    labels = {CYCLE_A: {"a1": "material", "a2": "noise"}, "no-such-cycle": {"zz": "noise"}}
    r = _run(tmp_path, _labels_file(tmp_path / "labels.json", labels))
    assert (r.items, r.labeled, r.labeled_decided, r.orphan_labels) == (4, 2, 2, 1)
    assert r.coverage == 0.5
    assert (r.tp, r.fp, r.fn, r.tn) == (1, 0, 0, 1)
    assert r.escalated == 2  # b1 escalated too but is unlabeled, so it counts toward volume, not quality
    assert r.fp_rate == 0.0 and r.noise_ratio == 0.0


def test_labels_loader_validates(tmp_path: Path) -> None:
    empty = load_labels(None)
    assert empty.by_key == {} and empty.hash.startswith("sha256:")
    good = load_labels(_labels_file(tmp_path / "ok.json", {CYCLE_A: {"a1": "material"}}))
    assert good.by_key == {(CYCLE_A, "a1"): "material"}
    same = load_labels(_labels_file(tmp_path / "ok2.json", {CYCLE_A: {"a1": "material"}}))
    assert same.hash == good.hash
    with pytest.raises(LabelsError):
        load_labels(tmp_path / "missing.json")  # a named file that is absent is not "no labels"
    with pytest.raises(LabelsError):
        load_labels(_labels_file(tmp_path / "bad.json", {CYCLE_A: {"a1": "maybe"}}))
    (tmp_path / "ver.json").write_text('{"schema_version": 2, "labels": {}}', encoding="utf-8")
    with pytest.raises(LabelsError):
        load_labels(tmp_path / "ver.json")


# -- report -----------------------------------------------------------------------------------------------------------


def test_template_has_placeholders_and_no_numbers() -> None:
    text = render_report(TriageConfig())
    assert "**Status:** _pending_" in text
    assert "| p95 latency per item | _pending_ | ≤ 10.0 s | _pending_ |" in text
    assert "| False-positive rate | _pending_ | ≤ 10.0% | _pending_ |" in text
    assert "sha256:" not in text


def test_report_reflects_only_measured_values(tmp_path: Path) -> None:
    r = _run(tmp_path, _labels_file(tmp_path / "labels.json", {CYCLE_A: {"a1": "material"}}))
    text = render_report(TriageConfig(), r, "python scripts/benchmark.py ...")
    assert r.corpus_hash in text and "python scripts/benchmark.py ..." in text
    assert "| p95 latency per item | 2.0000 s |" in text
    assert "| False-positive rate | _pending_ |" in text  # no noise items labeled: FP rate is unmeasurable, not 0%
    assert "| Coverage (labeled / items) | 25.0% |" in text


def test_template_cli_writes_report_without_measuring(tmp_path: Path) -> None:
    report = tmp_path / "baseline.md"
    assert main(["--workspace", str(tmp_path), "--template", "--report", str(report)]) == 0
    assert report.read_text(encoding="utf-8") == render_report(TriageConfig())
