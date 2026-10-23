#!/bin/sh
# Lint gate (mirrors .github/workflows/lint.yml). Skip with: git commit --no-verify
set -e
cd "$(git rev-parse --show-toplevel)"
.venv/bin/ruff check src/
.venv/bin/mypy --strict src/
