# stockmanager

Semi-autonomous **mock** portfolio manager. No real money, no brokerage: every fill is simulated against real session prices.

## Specs (repo root, source of truth)
`automated-portfolio-system-spec.md` (SS), `architecture-spec.md` (AS), `testing-spec.md` (TS), `user-experience-spec.md` (UX), `llm-prompts.md`, `news-sources.md`. Read the relevant spec section before changing behaviour.

## State
- File-backed only, no database. `workspace/` (gitignored) holds `portfolio.json` (source of truth) and `trades.log` (audit copy of every fill).
- Canonical JSON: `indent=2`, `ensure_ascii=False`, trailing newline. Money and shares are `Decimal`, serialised as strings, 4 dp, `ROUND_HALF_UP`. Never go through `float`.
- Timestamps are ISO 8601 in PT (`America/Los_Angeles`). Session open is 06:30 PT (09:30 ET).

## Prices (SS §6, AS §6)
- Yahoo Finance v8 chart API primary, Finnhub fallback. Source is recorded per fill (`price_source`).
- One source per valuation: if the primary fails for ANY ticker, redo the whole valuation on the fallback. Never mix.
- Fail closed: if neither source can price everything, leave the portfolio unchanged.
- Prices extracted from news/LLM text are never a price source.

## Stack and layout
Python 3.12+ (`.python-version` pins the local interpreter), pydantic v2 models (`extra="forbid"`), pytest + pytest-asyncio. Code in `src/stockmanager/`; `domain/` holds models and ports, `adapters/` the real providers, `app.py` is the composition root.

## Conventions
- Type hints on all public functions; docstrings on public modules, classes and functions.
- `ruff` (line length 120) and `mypy --strict` must pass: `make lint`. Tests: `make test`.

## Status
Phase 1: scaffolding and price pipeline. `pricing/` and `adapters/` are stubs.
