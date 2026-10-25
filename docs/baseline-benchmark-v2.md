# Baseline benchmark v2 — Q1 triage classifier (skeleton)

Gate for `triage.benchmark_passed` (test plan §4, R14). Every value below is **pending the >=4-week labeled corpus**;
nothing here is a measured result yet. Replace the values from the rerun output, never by hand-estimate.

**Verdict:** _pending the >=4-week corpus_ (latency half: probe evidence passes; FP half: unmeasured)

## Corpus

| Field | Value |
|---|---|
| Corpus hash | _pending the >=4-week corpus_ |
| Corpus window (first .. last fetched) | _pending the >=4-week corpus_ |
| Cycles / items | _pending the >=4-week corpus_ |
| Labels hash | _pending the >=4-week corpus_ |
| Labels coverage (labeled / items) | _pending the >=4-week corpus_ |
| Classifier | `LLMClassifier` (OpenAI-compatible, `reasoning_content` primary) — model _pending_ |

Day-0 file (2026-10-25, 2102 rows, 980 duplicates) is frozen and excluded (R17), archived unmodified at sha256:be6d6aa32b9cff1e293adb0a4f0d75073bdf6debf9718d4a2a73c32934248d41; the corpus measures from 2026-10-26.

## Results

| Criterion | Measured | Limit (`TriageConfig`) | Verdict |
|---|---|---|---|
| Per-item latency p50 | _pending the >=4-week corpus_ | informational | — |
| Per-item latency p95 | _pending the >=4-week corpus_ | ≤ `benchmark_item_p95_seconds` (10 s) | _pending_ |
| Batch latency p50 | _pending the >=4-week corpus_ | informational (R14: observed volume) | — |
| Batch latency p95 | _pending the >=4-week corpus_ | ≤ `benchmark_batch_p95_seconds` (60 s) | _pending_ |
| False-positive rate | _pending the >=4-week corpus_ | ≤ `benchmark_max_fp_rate` (10%) | _pending_ |
| Noise ratio | _pending the >=4-week corpus_ | ≤ `benchmark_max_noise_ratio` (20%) | _pending_ |
| Coverage | _pending the >=4-week corpus_ | — | — |

## Rerun

```
Q1_CORPUS_HASH=sha256:<corpus hash> Q1_HELD=<TICKER,...> TRIAGE_API_KEY=<key> \
  .venv/bin/pytest tests/benchmark_q1.py -m benchmark -s
```

Reads `workspace/fetch-log/`, `workspace/labels.json` and `workspace/config.yaml` (`triage.classifier_base_url`,
`classifier_model`, thresholds). Fails closed on an empty corpus, a missing or non-matching hash, or any status but
`pass`. The harness-only form (`python -m stockmanager.triage.benchmark --report docs/baseline-benchmark-v2.md ...`)
renders this table from a result.

## Probe evidence so far (R14, not the benchmark)

- Per-item latency: p50 3.7 s, p95 5.7 s (target ≤ 10 s: pass). Worst case at the 20-item cap: 73.7 s.
- Strict JSON parsed 8/8 when `max_tokens` was budgeted above the reasoning length.
- Response shape: the answer arrives in `message.reasoning_content` with `message.content` null. The adapter reads
  `reasoning_content` first and falls back to `content`; both shapes are covered in `tests/unit/test_llm_classifier.py`.
- The false-positive half cannot be judged from a probe: it waits on the labeled corpus.
