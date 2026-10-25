"""Q1 shadow-run benchmark (v2 test plan §4): replay a frozen fetch-log corpus through the triage classifier.

Thin entry point for ``stockmanager.triage.benchmark``; run it from the repo root with the project venv, e.g.::

    python scripts/benchmark.py --workspace workspace --labels workspace/benchmark/labels.json \\
        --held NVDA,AAPL --classifier my_pkg.models:build --expect-hash sha256:<hash> \\
        --report docs/baseline-benchmark-v2.md

Exit codes: 0 pass, 1 fail, 2 incomplete (unlabeled, unmeasurable or classifier down), 3 error (nothing written).
``--template --report docs/baseline-benchmark-v2.md`` writes the empty report with every value pending.
"""

from __future__ import annotations

from stockmanager.triage.benchmark import main

if __name__ == "__main__":
    raise SystemExit(main())
