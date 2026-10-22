# stockmanager: project postmortem

Snapshot of `main` @ `4f7c029` (34 commits). Claims below were checked against source on 2026-10-22. Spec = `architecture-spec.md` (AS, repo root, not `docs/`). Operator how-to lives in `docs/user-manual.md`; not repeated here.

## 1. Overview

A semi-autonomous **mock** portfolio manager: no brokerage, fills are simulated at real session prices. Once a week an LLM reasons over extracted news facts, proposals are posted to Discord, the owner has a veto window, and unvetoed trades fill at the next open. One owner, one workspace, one live portfolio (78 positions per the manual).

Stack: Python 3.14 (`.python-version`; `pyproject` allows >=3.12), pydantic v2. This is **not** "stdlib + pydantic": runtime deps also include `anthropic`, `yfinance`, `pandas`, `httpx`, `aiohttp`, `discord.py`, `jinja2`, `pyyaml`, `psutil`, `defusedxml`.

## 2. Module inventory (`src/stockmanager/`, 12,000 lines)

| Module | Responsibility / key symbols |
|---|---|
| `app.py` | Composition root: `create_app`, `init_workspace`, `load_secrets`, `AppContext` |
| `cli.py`, `commands/` | argparse CLI; `handlers.py`, `parser.py`, `auth.py` shared by CLI and Discord |
| `bot/bot.py` | discord.py daemon (the only `async` module); transport only |
| `scheduler/` | `tick.py` (`tick`, `Watchdog`, `tick_lock`), `scheduler.py` (`list_due_jobs`), `slots.py`, `sweeper.py`, `state.py` |
| `evaluation/` | `engine.py` (`EvaluationEngine.run`), `reasoner.py`, `reasoner_io.py`, `builder.py` (`ProposalBuilder`), `reconfirm.py`, `thesis_checker.py` |
| `execution/engine.py` | `ExecutionEngine.run_proposal` / `run_opening`: fills, ledger commits (599 lines, largest) |
| `news/` | `pipeline.py` (`run_news_stage`), `fetchers/{searxng,finnhub,yahoo_rss}`, `normalise`, `extract`, `validate`, `rank`, `ratelimit` (`CircuitBreaker`) |
| `llm/` | `protocol.py` (`LLMBackend`), `anthropic.py`, `openai_compat.py` |
| `pricing/` | `service.py` (`PriceService.valuation`, `fill_prices`), `validate.py`, `errors.py` |
| `adapters/` | `yfinance_provider.py` (wired), `yahoo.py` (v8 chart API, **not wired**), `finnhub.py`, `symbol_verifier.py`, `calendar.py`, `discord_rest.py`, `http.py` |
| `store/` | `FileStore`, `fsutil` (atomic write), `lock` (flock), `transaction.py`, `reconcile.py` |
| `domain/` | pydantic docs (`docs.py`), `ports.py`, `money.py`, `fills.py`, `metrics.py`, `proposal_state.py`, `sessions.py` |
| `notify/` | `outbox.py`, `deliver.py` (at-least-once, per-channel order), `templates.py`, `emit.py` |
| `dashboard/`, `config/`, `ops/doctor.py`, `sim/` | HTML/JSON report; pydantic `Config` + hot reload; cron check; test doubles |

## 3. Data flow

```
cron */5 -> tick (state/tick.lock, single-flight)
  reload config -> deliver_pending(outbox, 20s) -> sweep -> list_due_jobs (derived from books)
     |
     +-- evaluation slot (Sat 09:00 PT) -> EvaluationEngine.run
     |     A snapshot -> B (no lock) PriceService.valuation -> news stage -> Reasoner
     |     C lock + rev check -> ProposalBuilder -> Transaction -> stage alerts -> D dashboard
     |
     news stage: plan -> fetch (searxng > finnhub > yahoo_rss, threads) -> normalise
                 -> LLM Extractor -> validate -> rank -> extracted-facts/
     |
     +-- alerts/pending -> DiscordRESTChannel -> receipt starts veto window
     +-- execution job (execute_at + 5 min) -> ExecutionEngine -> fills at open price
         -> portfolio.json + trades.log
```

## 4. Design decisions (as implemented)

- **Protocol ports** (`domain/ports.py`, `llm/protocol.py`): tests inject `sim/` fakes; swapping LLM vendors is one line in `create_app` (`openai_compat.py` exists but nothing wires it).
- **Sync I/O**: the tick is a short-lived process; only `news/pipeline.py` uses a `ThreadPoolExecutor` and only the bot is async.
- **Files, not a DB**: atomic tmp+fsync+rename, `flock` for `portfolio.lock` and `tick.lock`, reconcile of `portfolio.json` vs `trades.log`.
- **Derived scheduling** (AS AD4): `list_due_jobs` is a pure function of books and config; a missed tick self-heals, and an evaluation slot completes only on `RunResult("completed")`.
- **Cooperative watchdog**: `Watchdog.check` runs between stages only, never mid-commit.
- **Tolerant reconcile** (`9ae7931`): at most 3 divergences, each a share-count delta <= 1 on a position open on both sides; anything else halts.

## 5. Quality metrics (measured)

- `pytest`: **1181 passed in ~22 s** (969 unit, 57 integration, 85 sim, 70 top-level). Test code is 12,566 lines, about equal to source.
- **Coverage not measured**: `pytest-cov` is not installed.
- **`make lint` fails**: ruff 14 errors, mypy --strict 44 errors in 12 files (mostly the execution-engine bug in 6.1, plus test fakes and the untyped `yfinance` import). `stockmanager-handoff.md` ("785 tests, lint clean") is stale.
- Invariants with tests: one-source valuation and fail-closed (`PriceService`), mixed-source rejection, Decimal money, retry of unfinished evaluation slots, outbox ordering, lock behaviour, sim fault injection (`tests/sim/`).

## 6. Known gaps and discrepancies

### 6.1 Defects

1. **Dashboard never refreshes after fills.** `execution/engine.py` lines 280, 448 and 570 call `write_dashboard(self._store, model, html)`; the signature is `write_dashboard(model, root, render, eval_pages)`. The `TypeError` is swallowed by `except Exception`, so only a log line shows it. Introduced in `54332f6`. Fix the calls to match `evaluation/engine.py:253`, then add a test that asserts the file is written.
2. `evaluation/engine.py:252`: `html` is assigned and unused (ruff F841).
3. `yfinance_provider.py` returns a TTL-expired cache entry when a fetch fails (`get_price`, `prime`). That bypasses the fall-back-everything rule in `PriceService`. Its docstring says "flagged `stale=True`", but `PriceQuote` has no such field. Raise on stale, or add and propagate the flag.

### 6.2 Spec vs code

| Spec | Code |
|---|---|
| CLAUDE.md: `pricing/` and `adapters/` are stubs | Both are implemented (Status section stale) |
| CLAUDE.md/AS §6: Yahoo v8 chart API primary | `_build_price_provider("yahoo")` returns `YfinancePriceProvider`; `yahoo.py` is used only for helpers by `finnhub.py` and `symbol_verifier.py` |
| AS §6.4: NYSE holiday calendar from `scripts/gen_calendar.py` | `SimpleWeekdayCalendar` (Mon-Fri, no holidays or early closes). `data/calendars/nyse.json` exists but is unused; `scripts/` and `schemas/` are empty |
| AS §2.3: 1-minute heartbeat | 5 minutes (documented in `ops/crontab.example`) |
| AS §1.8: import-linter contracts in CI | No import-linter and no equivalent test. `news/fetchers/*` import `adapters.http`; `store`-only writes are not enforced |

### 6.3 Actionable list

- [ ] Fix item 6.1(1) and add the regression test (highest value).
- [ ] Get `make lint` green: add `prime()` to remaining test fakes, `ignore_missing_imports` for `yfinance`, run `ruff --fix`.
- [ ] Add `pytest-cov`; record a baseline.
- [ ] Build the holiday-aware calendar. Until then, execution can be scheduled on market holidays.
- [ ] Decide the Yahoo path: delete `yahoo.py` or wire it; fix CLAUDE.md either way.
- [ ] Add an import-rule test (AST walk) for AS §1.8.

## 7. Runbook (summary)

- Setup: `make dev`, secrets in `~/.config/stockmanager/secrets.env` (`ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`, `DISCORD_BOT_TOKEN`; env `SEARXNG_URL`, default `http://localhost:8080`). `stockmanager init`, then install `ops/crontab.example` (or the launchd plist).
- Check: `stockmanager doctor` (cron/launchd), `stockmanager status`, `stockmanager news`, `stockmanager report`.
- Stuck evaluation: slot retries each tick unless `outcome` is `completed`; see `workspace/logs/tick.log`. A `halted_reconcile` raises a divergence alert and needs manual repair of `portfolio.json` / `trades.log`; `stockmanager ack` records review.
- Bot down: nothing is lost; the CLI runs identical handlers.

## 8. Insights

- **Swallowed exceptions hide integration bugs.** Defensive `try/except` around the dashboard hid a wrong signature since `54332f6`; mypy flags it, but lint was not gating commits. Make lint a pre-commit or CI gate.
- **Rate limits drove the price design.** The history runs v8 per-ticker with a 20 s delay (`e89e952`), a batch attempt (`41bca4c`) that Yahoo rejected (`de69a69` revert), then `yfinance` with a file cache (`a158b2d`, `300be5b`). With ~90 symbols per valuation, cache plus batching beats a retry loop.
- **Interface drift is silent without a type gate**: `prime()` was added to the `PriceProvider` protocol and test fakes broke twice (`1d09b43`, `4f7c029`).
- **What worked:** Protocol seams plus `sim/` fakes made 1181 offline tests cheap; Decimal strings everywhere avoided float drift; derived scheduling made retries trivial; the pure fail-closed `PriceService` is small and well covered.
- **Docs rot:** CLAUDE.md, the handoff and the file locations in the specs disagree with the code. Treat this file as a point-in-time audit and re-verify before relying on it.
