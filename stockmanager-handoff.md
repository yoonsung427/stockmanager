# stockmanager — Project Handoff

**Date:** Monday, October 19, 2026
**Last commit:** `133569e feat: scheduler, tick, sweeper, execution engine`
**Tests:** 785 passing | **Lint:** ruff + mypy --strict clean (114 source files)
**Branch:** `main`

## Completed (9 phases — all committed to main)

| Phase | Commit | What was built |
|-------|--------|---------------|
| 1. Scaffolding + Specs | `a695b84` | Repo structure, Makefile, lint, test config, spec files |
| 2. Price Pipeline | `156936c` | Yahoo v8 + Finnhub providers, PriceService, bar-based pricing, pricing errors, fail-closed protocol |
| 3. Portfolio State | `8e2cc42` | FileStore, atomic writes (tmp→fsync→rename), PortfolioLock (flock + stale PID break), Transaction (staged writes, fixed order), reconcile (suffix repair, torn line rebuild), LedgerState, domain models (PortfolioDoc, Position, Trade, ProposalDoc, TransitionRecord), Decimal money (q4, Q4), proposal state machine (LEGAL transitions, chain_hash, effective_state) |
| 4. News Pipeline | `b264894` | RSS fetchers, normalize, LLM extract (extractor input/output, prompts), validate, rank, persist |
| 5. Reasoning Engine | `0522276` | Reasoner protocol, AnthropicReasoner, ReasonerInput/Output, prompt loading, ProposalBuilder (intent→proposals with cap checks, pairing, sizing), proposal state machine |
| 6. Dashboard | `e8bfaaf` | Dashboard model (positions, trades, NAV, risk, config snapshot, jobs, alerts), build, write, render templates |
| 7. Discord Bot + Notifications | `1b69ab5` | Discord REST bot (gateway + intents), command parser, auth (allowed users), command handlers (STATUS, JOBS, ALERTS, SET, APPROVE, VETO, PAUSE, RESUME, CLOSE, RECONFIRM, REPLAY, DRY-RUN), outbox (enqueue, drain, dedup), delivery, alert templates |
| 8. Evaluation Engine | — | LLM evaluation engine: intake → analysis → Reasoner → ProposalBuilder → PROPOSED state |
| 9. Scheduler + Execution | `133569e` | Scheduler (slot computation, due jobs, all jobs), tick dispatcher (single-flight, config reload, outbox drain, sweep, dispatch), sweeper (PAUSED/CLOSED convergence, PROPOSED→VETO_WINDOW, auto-approve at deadline, expiry with alert, D10 late delivery), execution engine (run_proposal: pre-flight → group legs → prices → lock → re-validate → plan → void checks → idempotency → apply → commit; run_opening: all-or-nothing; run_final_valuation: close prices before lock, one transaction, positions NOT sold), fill math (size_buy, size_sell, clamp_to_cash, apply_fill), risk metrics (ROI, max_drawdown, sector_concentration, single_position_risk, benchmark_relative, volatility, Sharpe, correlation_matrix, momentum), config reload (watch mtime + hash, auto-reload) |

## Module inventory (what exists)

```
src/stockmanager/
  __init__.py           — package marker
  adapters/             — Yahoo v8 chart API, Finnhub REST, RSS adapters
  bot/                  — Discord bot, gateway, intents, message handling
  commands/             — parser (tokenize), auth (allowed users), handlers (all commands)
  config/               — model (Pydantic Config, SETTABLE_KEYS), LLM config, proposal config, config reload
  dashboard/            — model, build, write, templates (HTML + partials)
  domain/               — docs (PortfolioDoc, Position, Trade, ProposalDoc, etc.), ids, money (q4, Q4), ports (protocols), proposal_state, fills (size_buy/sell, clamp_to_cash, apply_fill), metrics (risk metrics)
  evaluation/           — engine (intake → analysis → Reasoner → ProposalBuilder), prompts, reasoner_io
  execution/            — engine (run_proposal, run_opening, run_final_valuation)
  ledger/               — stubs
  news/                 — fetchers (RSS), normalize, extract (LLM), validate, rank, persist
  notify/               — outbox (enqueue, drain), deliver (delivery logic), templates (alert rendering)
  ops/                  — operational utilities
  pricing/              — service (PriceService), errors (PriceError, ValuationUnavailable)
  prompts/              — extractor v1, reasoner v1 (prompt files)
  scheduler/            — scheduler (due jobs), slots (slot arithmetic), state (scheduler.json), sweeper, tick dispatcher
  sim/                  — simulation harness (stub)
  store/                — fsutil (FileStore, atomic_write), lock (PortfolioLock, Clock), transaction, reconcile
```

## What remains

### 1. CLI entry point (`app.py` + `__main__.py`)

Build the CLI that serves as the user-facing command interface and the cron hook.

**Specs:** AS §1.0 (overview), AS §1.3 (components), UX §2 (user experience)

**Commands:**
- `stockmanager tick` — run one tick (config path arg, workspace path arg)
- `stockmanager status` — print current portfolio summary + jobs
- `stockmanager stage <ticker>=<value> ...` — validate tickers via SymbolVerifier, write staged_orders[] (UX §2 steps 4-5)
- `stockmanager bot` — run Discord bot in foreground (for launchd/testing)
- `stockmanager dry-run` — run tick with DrySink (no writes, just log)
- `stockmanager replay <date>` — replay trades from log against prices for that date

**Composition root:** `app.py` wires all dependencies (store, clock, calendar, price providers, channels, execution engine, bot). The CLI imports from `app.py` and calls the right method.

**Key detail:** The tick command is what `hermes cron` invokes every minute. It must be fast — if the lock is held, it exits immediately (AS §1.5).

### 2. Cron integration

Create a `hermes cron` job that runs `stockmanager tick` every minute.

**Specs:** AS §1.5 (tick loop), SS §12 (scheduled operations)

The cron entry should point to the venv Python and invoke the CLI. Example:
```
* * * * * /path/to/venv/bin/python -m stockmanager tick --workspace /path/to/workspace
```

### 3. Evaluation engine wiring

The evaluation engine exists (`evaluation/engine.py`) but the full pipeline from news intake → Reasoner → ProposalBuilder → outbox alerts needs to be wired into the tick dispatch. Currently the tick dispatches an "evaluation" job but the handler is a stub.

**Specs:** AS §1.2 (evaluation phases), AS §7 (Reasoner), AS §8.2 (ProposalBuilder), SS §8 (intelligence pipeline)

**What to wire:**
- When tick dispatches an "evaluation" job, call `EvaluationEngine.run_evaluation()`
- This runs: intake (news facts for holdings) → analysis (metrics, NAV, risk) → Reasoner call → ProposalBuilder → stage proposals → enqueue alerts → update dashboard → save evaluation file → mark slot complete

### 4. Config model gaps

The config model (`config/model.py`) is mostly complete but verify against SS §11:
- ✅ `auto_approve`, `veto_window_hours`, `evaluation_frequency`, `evaluation_day`, `evaluation_time`
- ✅ `max_position_pct`, `position_alert_pct`, `max_trades_per_cycle`, `proposal_grace_minutes`
- ✅ `max_sector_pct`, `drawdown_alert`, `benchmark`
- ✅ `price_source_primary`, `price_source_fallback`
- ✅ `risk_free_rate_annual`
- Missing: `ops.visible_channels` (AS §3.6, default `[discord]`), `ops.notification_catalogue` (SS §10)

### 5. Reconfirm flow

`RECONFIRM <id>` command handler creates a new proposal immediately (new ID, explicit mode, `reconfirms` field). Validation — fresh valuation, cap re-check, Reasoner thesis re-check — runs as the next tick's derived job.

**Specs:** AS §8.7 (RECONFIRM semantics), TS CMD-RC01..06

### 6. Alert backfill

Sweeper re-derives missing `veto_window_open`, `evaluation_complete`, and `trade_executed` alerts from the books (deterministic alert IDs → idempotent enqueue).

**Specs:** AS §8.3 step 7

### 7. Receipt-driven veto window

AS §8.3: proposal created PROPOSED with no clock running. Veto window starts only after delivery receipt from a visible channel. Sweeper finds PROPOSED proposals with a receipt → transitions to VETO_WINDOW with `veto_deadline = receipt.veto_deadline`.

**Current simplification:** veto window opens on first sweep after creation (documented in sweeper docstring). To fully implement: add `delivery` field to ProposalDoc with receipts list.

**Specs:** AS §8.3 steps 1-6

### 8. Sim/harness tests

Crash-point fault injection tests (TS NN-20..29): kill at each commit point (CP0-CP7), verify next run recovers correctly. Edge case tests (TS EDGE-*): late delivery, pair unpriceable, etc. Negative tests (TS NEG-*): invalid symbols, cap breaches, etc.

**Specs:** TS §3.3 (fault points), TS §4 (negative tests), TS §5 (edge cases)

### 9. Integration tests

End-to-end flows: full evaluation cycle (news → Reasoner → ProposalBuilder → outbox), proposal lifecycle (PROPOSED → VETO_WINDOW → APPROVED → EXECUTED), close confirm (final valuation, proposals cancelled, status CLOSED).

**Specs:** TS §6 (integration tests)

## Spec files (source of truth)

- `automated-portfolio-system-spec.md` — system spec (SS §n)
- `architecture-spec.md` — architecture spec (AS §n)
- `testing-spec.md` — testing spec (TS §n)
- `user-experience-spec.md` — UX spec (UX §n)
- `llm-prompts.md` — prompt definitions
- `news-sources.md` — news config

## Key conventions

- **Money:** `Decimal` throughout; stored as strings with 4dp, `ROUND_HALF_UP`. Never `float` for money.
- **Timestamps:** ISO 8601 in PT (`America/Los_Angeles`). Session open: 06:30 PT (09:30 ET).
- **Atomic writes:** tmp file → fsync → rename → dir fsync (AS §8.5).
- **One write protocol:** all mutations through `Transaction` object.
- **Fail closed:** any doubt → no write, alert.
- **Idempotent:** ticks, deliveries, jobs, commands repeatable without double effects.
- **Time is injected:** no `datetime.now()` — use `Clock` seam.
- **LLMs at two seams only:** Extractor (news) and Reasoner (evaluation). No tools, no network, schema-constrained output.
- **ruff** (line length 120) + **mypy --strict** must pass. `make lint`, `make test`.

## Deviation log (known simplifications)

1. **Receipt-driven veto window:** simplified to first sweep after creation (no delivery/receipt field in ProposalDoc). Documented in sweeper module docstring.
2. **Alert backfill:** not implemented — alerts are staged inside transactions, so commit + alert are atomic.
3. **Reconfirm job:** not derived in scheduler — RECONFIRM creates a fully-formed proposal synchronously. Documented in scheduler module docstring.
4. **Config model:** intentional subset of full config surface; `ops.*`, `llm.*`, `news.*` are file-edit only.

## Running the project

```bash
cd ~/stockmanager
source .venv/bin/activate
make lint     # ruff + mypy --strict
make test     # pytest
```

## Next session priority

1. Build CLI entry point (`app.py`, `__main__.py`) — this enables the tick command for cron
2. Wire evaluation engine into tick dispatch
3. Add `hermes cron` job for tick
4. Add `ops.visible_channels` to config model
5. Reconfirm flow
6. Sim/harness tests (crash points)
