# Automated Portfolio Management System — Design Specification

**One-sentence version:** The Muse agent manages a configurable mock portfolio indefinitely. All state lives in plain workspace files. Prices and news are gathered at each evaluation cycle, the agent reasons over that evidence, proposes trades with risk analysis, and executes them — with the user able to veto any trade or pause the system at any time.

**Status:** Draft — for review before implementation.
**System:** Semi-autonomous. The agent evaluates, proposes, and executes trades automatically. The user can veto, override, or pause at any time. No trade is irreversible.

**Money:** None. Every fill is a mock fill. There is no brokerage account, no order routing, and no real-money movement anywhere in the design.

**How to read this document:** Sections 1-3 define the system scope and architecture. Sections 4-7 define data, lifecycle, pricing, and execution. Section 8 is the intelligence pipeline — the core differentiator from the prior design. Section 7.1 defines the write protocol. Sections 8.5-8.6 define the untrusted-input boundary, trade limits, and proposal expiry. Sections 9-10 cover monitoring and alerts. Sections 11-13 cover configuration, operations, and integrity.

---

## 1. Purpose, scope, and non-goals

An ongoing, AI-managed mock portfolio that continuously evaluates market conditions, news catalysts, and portfolio risk — then acts on that analysis. No fixed end date, no fixed starting budget. The user sets initial conditions and the system runs until stopped.

### Explicit non-goals

**Not a real trading platform.** No brokerage is connected, no real order can be placed, and no credentials for one are held.

**Not fully autonomous.** The agent proposes and executes, but the user retains veto power, can override allocations, and can pause the system at any time.

**Not a rules engine.** There are no hardcoded buy/sell triggers — no "trim at +10%" or "exit at -8%" logic. Judgement is exercised by the agent, in language, over evidence it has just gathered (Section 8).

**Not a tracker for other portfolios.** This system manages one portfolio. External benchmarks (S&P 500, sector ETFs) are tracked for context only.

---

## 2. System architecture

Five components, same foundation as before but extended with intelligence and monitoring layers:

| Component | Function |
|---|---|
| **Goal** | Durable container: description, scheduled jobs, working files. |
| **Workspace state files** | Entire portfolio state as plain files (Section 4). Files, not a database — any run starts by reading them, ends by updating them. |
| **Scheduled jobs** | Recurring evaluation cycles (Section 11). One job per timed event, firing on schedule. |
| **Data ingestion layer** | Market prices, news headlines, earnings calendar, macro indicators gathered at each cycle (Section 8). |
| **Main chat + Discord** | Delivery and control channel. Reports, proposals, and alerts arrive here; vetoes, pauses, and overrides are given here (or via CLI) — never via the dashboard (Section 9). |
| **Web dashboard** | Read-only static HTML visualization — holdings, performance, risk metrics, recent activity (Section 9). It displays state; it cannot change it. |

Because each run re-reads the files and re-ingests fresh data, a run that is interrupted leaves the books in their last complete state — a job either finishes its file updates or the portfolio is unchanged.

---

## 3. Roles and authority

| Role | What they do | What they cannot do |
|---|---|---|
| **User — owner and veto authority** | Sets initial budget, approves or vetoes any trade, overrides agent decisions, pauses or resumes the system, closes the portfolio at any time. | — |
| **Agent — evaluator and manager** | Gathers prices and news, analyzes the portfolio, values positions, computes risk metrics, proposes trades with reasoning, executes approved/autonomous trades as mock fills, generates dashboard updates. | Execute any trade the user has vetoed; invent a price; backdate a fill; override a user pause. |
| **Scheduled jobs — timers** | Wake the agent at each evaluation cycle with a fixed instruction set (gather data, analyze, propose, execute). | Decide anything. A job carries instructions written in advance; it holds no discretion beyond them. |

**Veto means explicit opt-out.** The agent executes trades by default after proposal unless the user explicitly vetoes within the veto window. The user can also set `auto_approve: false` in configuration to require explicit approval for every trade (Section 11). Vetoes are accepted only from an allow-listed Discord user ID (or CLI/main chat) and must cite the proposal ID (Section 8.5). Per-cycle trade count and position-size caps bind the agent regardless of what the evidence suggests (Section 8.5).

---

## 4. Data model — where the truth lives

| Store | Contents and role |
|---|---|
| **portfolio.json** | Single source of truth. Fields: `start_cash` (configurable), `cash`, `status` (lifecycle gate, Section 5), `staged_orders[]` (ticker, target_pct, target_value, thesis), `positions[]` (ticker, shares, fill_price, fill_time, cost_basis, thesis, price_source), `trades[]` (structured copy of every fill, each carrying its `trade_id`), `config` (starting budget, auto_approve setting, evaluation frequency). `positions[]` is a derived view of `trades[]` and must be reproducible from it (Section 7.1). |
| **trades.log** | Append-only human-readable mirror. One line per fill: trade ID, timestamp, action, ticker, shares, fill price, cost, price source, ending "mock fill, no real money moved." Lines are never edited or removed. |
| **RULES.md** | Trading rules (long-only US stocks/ETFs, fractional shares, cash non-negative, no transaction costs, dividends ignored, no backdating). Stored beside the portfolio. |
| **eval-YYYY-MM-DD.md** | One dated evaluation per cycle: price table, totals, risk metrics, news with sources, agent reasoning, and any trade proposals. The audit record and the reasoning template. |
| **run-notes/** | Per-execution notes: what was read, which prices were used, what was written. Written for fills and rebalances. |
| **proposals/** | One JSON file per trade proposal, keyed by proposal ID: legs, sizing, evaluation reference, `created_at`, `veto_deadline`, `expires_at`, approval mode, and state (Section 8.6). |
| **portfolio.lock** | Advisory file lock held by any job that writes portfolio state (Section 7.1). |
| **extracted-facts/** | Structured facts extracted from fetched news/search text for each cycle (Section 8.5). Written before reasoning begins. |
| **Cron definitions** | Saved instruction text of every scheduled job — inputs, steps, guards, outputs. |
| **dashboard/** | Read-only static HTML/JSON files for the web dashboard (Section 9), regenerated at each evaluation cycle. |

---

## 5. Lifecycle and state gates

**States:** `STAGED` → `APPROVED` → `LIVE` → `PAUSED` → `LIVE` → `CLOSED`

| State | Meaning |
|---|---|
| **STAGED** | Opening orders written to portfolio.json with target dollars and theses. Nothing is invested yet. |
| **APPROVED** | User approves the initial picks. Only this status unlocks the opening fills job. |
| **LIVE** | Portfolio is active. Fills written, cash updated. Evaluation cycles run; trades execute via the pipeline (Section 8). |
| **PAUSED** | User has paused the system. Pausing cancels all open veto windows and pending execution jobs (Section 8.6). Scheduled jobs fire but report that the portfolio is paused and take no action. User can resume to return to LIVE; cancelled proposals are not revived. |
| **CLOSED** | User closed the portfolio. Final valuation computed. No further runs scheduled. |

**Lifecycle commands.** State changes are driven by short text commands, accepted from Discord (allow-listed account) or main chat/CLI, with the reply rules in Section 10:
- `APPROVE OPENING`: STAGED → APPROVED (approves the initial picks).
- `PAUSE` / `RESUME`: LIVE ↔ PAUSED.
- `CLOSE`: requests closing. The system replies asking for confirmation and changes nothing until `CLOSE CONFIRM` is received, which runs the final valuation and moves the state to CLOSED.

The gate is literal. A job reads portfolio.json and proceeds only if status matches its requirement. If the status is wrong, the job does nothing except notify the user. The gate is checked twice: when the job starts and again immediately before any write, after the lock is acquired (Section 7.1).

---

## 6. Pricing and fill rules

**Session prices only.** Buys at the start of a session fill at that session's **official open**; valuations use the **official close** of the stated day. An intraday, after-hours, or previous-session price is never substituted.

**One primary, one fallback, source recorded per fill.** Prices come from one primary source (Yahoo Finance) with one fallback (Finnhub). The source actually used is recorded per fill in both portfolio.json (`price_source`) and trades.log. LLM-extracted prices from search or news text are never a fill or valuation source; they may appear in the evaluation file as context only, labelled as unverified.

**No mixed sources within a valuation.** A single valuation (one cycle's price table and all totals, ROI, and risk metrics derived from it) uses one source for every ticker, including the benchmark. If the primary fails for any ticker, the whole valuation is re-run on the fallback, not patched ticker by ticker. If the fallback cannot price every ticker, the valuation fails closed. A paired fill (Section 7) likewise uses one source for both legs. Each valuation records its single source in the evaluation file. Fills are different: a fill is priced per ticker, so a mixed-source fill set is allowed only across independent single-leg fills, and each is recorded with its own source.

**Fractional sizing:** shares = target value / fill price, kept to four decimal places.

**Timestamp discipline:** fills carry the session open time — 06:30 Pacific (09:30 Eastern) — not the time the job happened to run.

**Fail closed:** if an official price cannot be established from the primary source, try the fallback source. If neither works, do not guess, and do not use a search-extracted price. Leave the portfolio unchanged and report that the fill could not be priced.

**Price sources:**
| Priority | Source | Method |
|---|---|---|
| 1 | Yahoo Finance v8 API (`query2.finance.yahoo.com/v8/finance/chart/`) | Programmatic, returns `meta.regularMarketPrice` and `meta.chartPreviousClose`. |
| 2 | Finnhub | Live quote endpoint. |
| — | Search-finance vertical | **Not a price source.** Context only (e.g., confirming a halt or corporate action). Never used for fills or valuations. |

---

## 7. Execution rules

**Proceeds-funded rebalancing.** One holding funds another — the portfolio stays fully invested and cash stays non-negative by construction.

**Fractional position adjustments.** Trades are sized as fractions of a position, not all-or-nothing, unless the agent determines a full exit is warranted.

**Paired fills.** A sell and its corresponding buy are both timestamped and recorded together, with the sell proceeds funding the buy.

**No backdating.** Every trade is timestamped at the session's official open or close. Deciding on Saturday does not earn Friday's price.

**Append-only logs.** trades.log is never edited. portfolio.json trades[] array is append-only.

### 7.1 Write protocol

Every job that changes portfolio state follows this protocol. It supersedes any looser reading of "a job either finishes its file updates or the portfolio is unchanged" (Section 2) by defining how that is achieved.

1. **Trade ID per proposal.** Every proposal gets a unique `proposal_id` at creation (Section 8.6). Every fill derived from it gets a `trade_id` of the form `<proposal_id>-<leg>` (e.g., `P-2026-10-24-01-sell1`). Before writing, the execution job checks that no entry in `trades[]` or `trades.log` already carries that `trade_id`; if one exists, it skips the fill. This is the idempotency guard referenced in Section 15.
2. **File lock.** Before reading state for a write, the job acquires `portfolio.lock` (exclusive, non-blocking with a short retry, e.g., 3 attempts over 30 s). Evaluation and execution jobs cannot write simultaneously. If the lock cannot be acquired, the job aborts without writing, notifies the user, and does not fall back to unlocked writes. A lock older than a stale threshold (default 15 minutes) with a dead owner PID may be broken, and the break is logged.
3. **Re-check under lock.** After acquiring the lock, re-read portfolio.json and re-check the lifecycle gate (Section 5), proposal state, and expiry (Section 8.6). State read before the lock is discarded.
4. **Temp file, then atomic rename.** Each file is written to a temp file in the same directory (e.g., `portfolio.json.tmp`), flushed and fsynced, then renamed over the target. A reader never sees a half-written file. trades.log lines are appended and fsynced.
5. **Fixed write order.** `portfolio.json` is written first, then `trades.log`, then the proposal state file, then run-notes, then dashboard files. portfolio.json is the source of truth, so a crash between steps leaves the log behind the state, never ahead of it. A log line without a matching `trades[]` entry is itself a divergence.
6. **Reconcile at startup.** Every job, before doing anything else, rebuilds positions and cash from `start_cash` plus the full `trades[]` array and compares the result with `positions[]` and `cash` in portfolio.json. It also compares `trades[]` with `trades.log` by `trade_id`. On any mismatch (shares or cash beyond four-decimal rounding, missing or extra trade IDs): the job stops, sets no new status, writes no trades, and sends a Discord alert and chat message naming the divergence. A trade that is in `trades[]` but missing from `trades.log` may be repaired automatically by appending the missing log line (logged in run-notes); every other divergence needs the user's decision. While halted, the user may reply `RECONCILE rebuild` to recompute positions and cash from `start_cash` plus `trades[]` and resume; nothing is rebuilt without that command.

---

## 8. Intelligence pipeline — the evaluation engine

This is the core differentiator from the prior design. The agent doesn't just gather prices and news — it performs structured analysis across five dimensions before proposing any trade.

### 8.1 Data ingestion (at each evaluation cycle)

| Data type | Source | Frequency |
|---|---|---|
| **Prices** | Yahoo Finance (primary) or Finnhub (fallback) per holding + S&P 500 + sector ETFs; one source for the whole valuation (Section 6) | Every cycle |
| **News** | Search-news vertical per holding + sector-wide headlines — untrusted text, passed through fact extraction (Section 8.5) before reasoning | Every cycle |
| **Earnings calendar** | Search-finance for upcoming earnings dates within 30-day window — untrusted text, extracted per Section 8.5 | Every cycle |
| **Macro indicators** | Key rates, inflation prints, Fed signals, major indices | Weekly |
| **Analyst actions** | Target changes, rating upgrades/downgrades on holdings | Every cycle |
| **Sector rotation** | Relative strength of sector ETFs (XLK, XLF, XLV, XLE, etc.) | Every cycle |

### 8.2 Analysis dimensions

The agent evaluates each holding across five dimensions:

**1. Thesis integrity.** Has the original investment thesis been confirmed, weakened, or broken? Evidence: news headlines, analyst actions, earnings results.

**2. Momentum and trend.** Return since fill, short-term momentum (1-week, 2-week), moving average position, relative strength vs benchmark.

**3. Catalyst proximity.** Earnings dates, product launches, regulatory decisions, macro events within the next 2-4 weeks. A dated catalyst inside the evaluation window increases conviction.

**4. Risk contribution.** Position weight vs portfolio, correlation with other holdings, sector concentration. A position that grows beyond its intended weight increases concentration risk.

**5. Opportunity cost.** Is capital in this position the best use, or would it earn more in another holding? Evaluated against thesis integrity and catalyst proximity of alternatives.

### 8.3 Risk metrics (computed each cycle)

| Metric | Formula | Alert threshold |
|---|---|---|
| **Portfolio ROI** | (current value - start_cash) / start_cash | Reported every cycle |
| **Max drawdown** | Peak-to-trough decline since inception | Alert at >5% |
| **Sector concentration** | Max sector weight across holdings | Alert at >80% |
| **Single-position risk** | Largest position as % of portfolio | Alert at >40% (`position_alert_pct`, drift from price moves). Separate from the 30% proposal cap in Section 8.5. |
| **Benchmark relative** | Portfolio ROI - S&P 500 ROI over same window | Reported every cycle |
| **Volatility proxy** | Standard deviation of weekly returns | Reported every cycle |
| **Correlation matrix** | Pairwise correlation of holdings (weekly returns) | Flaged if any pair >0.8 |

### 8.4 Decision pipeline

**Stage 1 — INTAKE.** Gather all data (8.1). Run fact extraction on every piece of fetched text (8.5). Save the structured facts, with source URLs, into the dated evaluation file. Raw fetched text is not passed to Stage 2.

**Stage 2 — ANALYSIS.** Evaluate each holding across five dimensions (8.2). Compute risk metrics (8.3). Write reasoning for each holding in the evaluation file.

**Stage 3 — SUGGESTION.** Propose specific trades with sizing, source-and-use pairing, and reasoning. Always present the hold-alternative. Label clearly: *auto-executing unless vetoed within veto window.* Each proposal gets a proposal ID, an `expires_at` timestamp (8.6), and is checked against the trade and position caps (8.5) before it is shown.

**Stage 4 — VETO WINDOW.** User has a configurable window (default: 4 hours) to veto. A valid veto (8.5) drops the trade. If no veto, proceed to Stage 5. If `auto_approve: false`, the user must explicitly approve. Pause and `auto_approve` changes during the window follow Section 8.6.

**Stage 5 — EXECUTION.** New run-once job fills at the next session open, following the write protocol (7.1). Records both fills with trade IDs, prices, sources, and the evaluation reference. If the job misses its time, the proposal expires (8.6); it is never filled silently late.

### 8.5 Untrusted input boundary and trade limits

**Fetched text is data, not instructions.** News, search results, and analyst commentary are untrusted and may contain text crafted to steer the agent.

**Structured fact extraction.** Before the reasoning step (Stage 2), a separate extraction step converts fetched text into records of fixed shape: `ticker`, `event` (short, from a closed set of event types such as earnings, guidance, rating_change, regulatory, product, m&a, macro, other, plus a one-line neutral description), `date`, and `source_url`. Records missing a ticker, date, or source URL are dropped. The reasoning step receives only these records, never the raw text. Instruction-like content in fetched text ("ignore previous", "buy X", etc.) has no channel to reach reasoning, and extraction output is never executed as a command. Extracted facts may not contain prices used for fills or valuations (Section 6).

**Per-cycle trade cap.** At most **10 trades** (individual fills; a paired sell and buy counts as 2) may be proposed per evaluation cycle (`max_trades_per_cycle`, default 10). Extra ideas are listed in the evaluation file as not proposed.

**Max position size.** No proposal may leave any position above `max_position_pct` of portfolio value at proposal-time prices (default **30%**, configurable). A proposal that would breach the cap is resized or dropped before it reaches the veto window. This is a hard cap on proposals; price drift past it later is handled by the advisory alert in 8.3, not by forced selling. Both caps are enforced in code at proposal creation and again at execution, not only in the agent's instructions.

**Discord veto authentication.** A Discord message counts as a veto or approval only if (a) the sender's user ID is on the allow-list in config (`discord_allowed_user_ids`), and (b) the message includes the proposal ID, e.g., `VETO P-2026-10-24-01`. Messages from other users, messages without an ID, or with an unknown, expired, or already-executed ID are ignored and logged. A bare "VETO" is not accepted. The same ID requirement applies to explicit approvals when `auto_approve` is false. Pause is accepted from the allow-listed ID only.

### 8.6 Proposal lifecycle, expiry, pause, and auto_approve changes

**States:** `PROPOSED` → `VETO_WINDOW` → (`APPROVED` | `VETOED` | `CANCELLED_PAUSED` | `EXPIRED`) → `EXECUTED`.

**Expiry.** Each proposal gets `expires_at` at creation: the scheduled execution time plus a grace period (default 60 minutes after the 06:30 PT open, `proposal_grace_minutes`). A proposal is only valid to fill inside [execution time, `expires_at`].

**Veto window starts after delivery.** The veto window (and, for `auto_approve`, the clock toward auto-approval) starts only once the proposal has been delivered to at least one visible channel (Discord or main chat). If neither channel works, the proposal waits in `PROPOSED`; nothing auto-executes unseen. `expires_at` still applies, so an undelivered proposal that passes it is `EXPIRED` under the rules below.

**Missed execution.** If the execution job did not run by `expires_at` (outage, lock failure, price failure), the proposal is marked `EXPIRED`. It is voided, or re-confirmed at the next session, and never filled silently:
- *Void (default):* the user is notified; nothing is filled.
- *Re-confirm:* the user may request re-confirmation. The agent creates a new proposal with a new ID that references the expired one, re-checks it against current prices, caps, and thesis, and requires explicit approval regardless of `auto_approve`. It fills at the next session's open only if approved before that session and inside the new expiry.

**Pause semantics.** Pausing (Section 5) cancels every open veto window and every pending execution job. Affected proposals become `CANCELLED_PAUSED`, and the user is notified listing them. Resume does not revive them; a later evaluation cycle proposes afresh. An execution job that fires while PAUSED (for example, if cancellation failed) does nothing, because of the second gate check under lock (7.1).

**Toggling `auto_approve` mid-window.** The change never executes a trade the user hasn't had the chance to stop:
- *true → false:* all open veto windows convert to explicit-approval-required. Window expiry no longer approves them. If not approved by `expires_at`, they expire.
- *false → true:* proposals already awaiting approval stay awaiting explicit approval. They are not auto-approved. The new setting applies only to proposals created after the change.
- The approval mode in force is recorded on each proposal, and the change is noted in the evaluation file.

---

## 9. Monitoring — web dashboard

A **read-only static HTML** dashboard, regenerated at each evaluation cycle, served locally via the Hermes desktop app preview pane. It displays state only. It contains no buttons, toggles, forms, or write path, and it cannot pause, veto, approve, or change configuration. Controls live in main chat, Discord, or the CLI (Sections 8.5, 10).

### 9.1 Dashboard layout

| Section | Content |
|---|---|
| **Summary** | Portfolio value, ROI, benchmark relative, max drawdown, days active |
| **Holdings table** | Ticker, thesis, shares, fill price, current price, value, return %, weight % |
| **Performance chart** | Portfolio value over time vs S&P 500, line chart |
| **Sector allocation** | Donut chart of sector weights |
| **Risk metrics** | Max drawdown, sector concentration, single-position risk, volatility |
| **Recent activity** | Last 10 trades with timestamp, action, ticker, shares, fill price |
| **Earnings calendar** | Upcoming earnings dates for holdings within 30 days |
| **News feed** | Recent headlines per holding with source links |
| **Evaluation log** | Link to each dated evaluation file with summary |
| **System status** | Read-only indicators: lifecycle status (LIVE/PAUSED), current `auto_approve` value, pending proposals with their ID, veto deadline, and expiry. Each shows the chat/Discord/CLI command needed to act, as text. |

### 9.2 Technical approach

- Dashboard is a self-contained, read-only HTML file with embedded CSS/JS. JS is for rendering only: no network writes, no form submission, no control elements
- Data loaded from JSON sidecar file regenerated each cycle
- Charting via lightweight inline SVG or canvas
- Served via `::preview{file="dashboard/portfolio.html"}` directive in Hermes desktop
- No external dependencies — works offline, no CDN calls
- Generated after portfolio files are written, so it never shows state ahead of the books (Section 7.1)

### 9.3 Dashboard refresh

Regenerated at each evaluation cycle. The Hermes desktop app preview pane auto-refreshes when the file changes. Between cycles, the dashboard shows last-cycle data with a "last updated" timestamp.

---

## 10. Alerts — Discord notifications

Discord bot sends structured alerts for key events:

| Trigger | Alert content |
|---|---|
| **Evaluation cycle complete** | Summary: portfolio value, ROI, best/worst holding, benchmark relative, any proposed trades |
| **Trade executed** | Ticker, action, shares, fill price, price source, resulting position weight |
| **Risk alert** | Metric name, current value, threshold, affected holding(s) |
| **Veto window opening** | Proposal ID, trade details, reasoning summary, veto deadline, expiry, "Reply `VETO <proposal_id>` from an authorized account to cancel" |
| **Earnings approaching** | Ticker, earnings date, pre-earnings price, position weight |
| **System pause/resume** | Status change confirmation, plus the list of proposals cancelled by a pause |
| **Proposal expired / voided** | Proposal ID, reason (missed execution, pause, price failure), and re-confirm instructions |
| **Reconcile divergence** | Which check failed (positions vs trades, trades vs log), amounts, and that the job halted |
| **Rejected control message** | Sender not allow-listed or missing/invalid proposal ID (logged, not acted on) |
| **Price source failure** | Affected ticker(s), fallback used or failure notice |

Alerts use Discord embeds with color coding: green for positive, red for alerts, blue for informational.

**Command vocabulary.** The same commands work from Discord and main chat/CLI:

| Command | Effect |
|---|---|
| `STATUS` | Current state, pending proposals, next scheduled run |
| `APPROVE <proposal_id>` / `APPROVE OPENING` | Approve a proposal, or the initial picks (Section 5) |
| `VETO <proposal_id> [reason]` | Cancel a proposal; the optional reason is recorded |
| `PAUSE` / `RESUME` | Freeze or unfreeze all activity (Section 5) |
| `SET <setting> <value>` | Change a setting (Section 11) |
| `RECONFIRM <proposal_id>` | Request a fresh proposal to replace an expired one (Section 8.6) |
| `RECONCILE rebuild` | Rebuild positions and cash from the trade history after a divergence halt (Section 7.1) |
| `CLOSE` / `CLOSE CONFIRM` | Request, then confirm, final valuation and closing (Section 5) |

**Every command gets a reply.** Each user command triggers an immediate reply: ✅ accepted, or ❌ with the reason. Rejection types include "Unknown or expired proposal ID", "Already executed", and "Sender not authorized". Rejected control messages are logged and not acted on.

**Outage queueing.** During a network or Discord outage, alerts are queued and replayed in order, with their original timestamps, when connectivity returns.

---

## 11. Configuration

| Setting | Default | Description |
|---|---|---|
| **start_cash** | 50000 | Initial portfolio budget (configurable before the portfolio goes live; cannot be changed on a running portfolio, since it is the return baseline) |
| **auto_approve** | true | If true, trades auto-execute after veto window. If false, explicit approval required. |
| **veto_window_hours** | 4 | Hours the user has to veto a proposed trade. |
| **evaluation_frequency** | weekly | How often evaluations run: `daily`, `weekly`, `biweekly` |
| **evaluation_day** | Saturday | Day of week for weekly/biweekly evaluations. |
| **evaluation_time** | 09:00 PT | Time for evaluation cycles. |
| **max_position_pct** | 30 | Hard cap on any position after a proposed trade, at proposal-time prices (Section 8.5). Configurable. |
| **position_alert_pct** | 40 | Advisory alert when drift pushes a position above this weight (Section 8.3). Not a cap. |
| **max_trades_per_cycle** | 10 | Max fills proposed per evaluation cycle (Section 8.5). |
| **discord_allowed_user_ids** | (none) | Discord user IDs permitted to veto, approve, or pause. Empty means Discord control is disabled. |
| **proposal_grace_minutes** | 60 | Minutes after the scheduled execution time before a proposal expires (Section 8.6). |
| **max_sector_pct** | 80 | Max sector concentration — advisory alert, not a hard cap. |
| **drawdown_alert** | 5 | Max drawdown % that triggers an alert. |
| **benchmark** | SPY | Benchmark ticker for relative performance. |
| **price_source_primary** | yahoo | Primary price source: `yahoo` or `finnhub`. `search` is not permitted. |
| **price_source_fallback** | finnhub | Fallback price source (`yahoo` or `finnhub`, different from primary). |

Configuration stored in portfolio.json `config` field and in a separate `config.yaml` in the workspace directory.

---

## 12. Scheduled operations

Every timed action is a **scheduled job** owned by the goal, in America/Los_Angeles time, delivering to the main chat.

| Schedule | Job | Does |
|---|---|---|
| **Opening (user-defined)** | Opening fills | Prices and writes opening fills, sets status to "live". |
| **Per evaluation_frequency** | Evaluation cycle | Values at most recent close, benchmarks, gathers data (Section 8.1), computes risk (8.3), reasons over evidence, proposes trades, auto-executes if no veto. |
| **Per trade proposal** | Trade execution | Created after veto window. Fills at next session open, records both fills. Acquires the lock, reconciles, and checks proposal expiry and status first (Sections 7.1, 8.6). Cancelled on pause. |
| **On demand** | Final valuation | User requests final valuation. Values all positions, computes final ROI and benchmark comparison, writes final report. No further runs scheduled. |

Execution jobs fire just after the open (06:35-06:40 PT) so the opening price exists to look up; fills are timestamped at the open. Evaluation and execution jobs never write at the same time (portfolio.lock, Section 7.1). A job that misses its time is not retried into a later session; the proposal expires (Section 8.6).

---

## 13. Decision-making principles

**Hold is the default.** A proposal must beat doing nothing. Every evaluation presents the hold alternative alongside any trade idea.

**A thesis, not a price, is what gets re-examined.** A falling holding is a candidate to *add to* if its thesis is intact, and a candidate to exit if the news breaks the thesis. A rising holding is a candidate to trim when the rise comes from a temporary premium rather than improved prospects.

**Trade strength into weakness, in pairs.** Rebalances are framed as source-and-use: one holding funds another, keeping the portfolio fully invested and cash non-negative by construction.

**Size moves in fractions of a position, not all-or-nothing.** Partial trims keep a working thesis in the portfolio while still freeing meaningful proceeds.

**Risk-aware sizing.** The agent considers position weight, sector concentration, and correlation before sizing a trade. A proposed addition that would push a position over the advisory max triggers a reduced sizing recommendation.

---

## 14. Weekly evaluation procedure

Each evaluation cycle runs the same steps, in order:

1. **Guard:** acquire portfolio.lock, run the startup reconcile (Section 7.1), read portfolio.json; stop unless status is "live" (or report that it is paused). Expire any proposals past `expires_at` (Section 8.6).
2. **Price:** for each holding and the benchmark, look up the most recent official close via the primary price source — one query per ticker. If any ticker fails, redo the whole valuation on the fallback (Section 6); never mix sources within the valuation.
3. **Data ingestion:** gather news, earnings calendar, analyst actions, sector ETF performance, macro indicators (Section 8.1), then extract structured facts from fetched text (Section 8.5).
4. **Value:** compute position values, total value, ROI, and benchmark comparison.
5. **Risk:** compute risk metrics (Section 8.3). Alert if thresholds exceeded.
6. **Analyze:** evaluate each holding across five dimensions (Section 8.2). Write reasoning in the evaluation file.
7. **Propose:** suggest any rebalance ideas with sizing, pairing, and reasoning, within the 3-trade and position-cap limits (Section 8.5). Assign proposal IDs and expiry times. Present hold alternative.
8. **File:** write the dated evaluation file with the full price table, risk metrics, headlines with sources, reasoning, and proposals. Written *before* the chat message, so the record exists even if the summary is skimmed.
9. **Execute or wait:** if auto_approve is true, open the veto window. If no valid veto (allow-listed user, proposal ID) arrives within the window, create the execution job. If auto_approve is false, wait for explicit approval. Behavior on pause or an `auto_approve` toggle follows Section 8.6.
10. **Report:** send a short chat update and Discord alert — value, ROI, best and worst holding, benchmark relative, any proposed trades.
11. **Dashboard:** regenerate the read-only web dashboard with updated data, then release the lock.

**Missed runs.** If an evaluation was missed (for example, the machine was asleep at `evaluation_time`), it runs once on wake, labeled "ran late", using the latest official closing prices. Missed cycles are not run repeatedly to catch up.

---

## 15. Integrity, failure modes, and limits

**Every fill is auditable:** timestamp, shares to four decimals, fill price, cost, named price source, and the mock-fill note — in both the structured record and the append-only log.

**No silent trades:** evaluation jobs propose but cannot trade without the veto/approval pipeline. Execution jobs exist only after the veto window closes or explicit approval is given. The idempotency guard (trade ID check, Section 7.1) prevents duplicated trades. Expired proposals are voided or re-confirmed, never filled late (Section 8.6).

**One primary, one fallback, source per fill:** Yahoo Finance primary, Finnhub fallback, source recorded per fill. A valuation never mixes sources. LLM-extracted search prices are never used. Fixes the multi-source variance of the prior design.

**Write integrity:** locked, atomic, ordered writes and a startup reconcile that rebuilds positions from trades (Section 7.1). Divergence halts the system rather than being papered over.

**Prompt-injection containment:** fetched text reaches reasoning only as structured facts; trades are capped per cycle and per position; control messages need an allow-listed user ID and a proposal ID (Section 8.5).

**Read-only dashboard:** no control surface exists on the dashboard, so a compromised or spoofed page cannot veto, pause, or approve anything.

**Fail-closed pricing:** if neither primary nor fallback source can establish a price, the job does not invent one. It leaves the portfolio unchanged and reports the failure.

**Veto trail:** every vetoed trade is logged in the evaluation file with the user's reasoning if provided. The agent learns from vetoed trades — if a thesis is repeatedly vetoed, it deprioritizes similar proposals.

**Reasoning is judgement, not a guarantee:** the pipeline preserves the evidence and the reasoning for every suggestion, so a bad call can be examined afterwards. The five-dimension analysis framework provides structure for post-hoc review.

**Simplifications by design:** dividends and transaction costs are excluded, keeping results comparable across mock portfolios. This is not a simulation of a real brokerage account.

---

## 16. Rebuilding this faithfully

A reimplementation on any platform preserves the parts that make the result trustworthy:

1. The trading rules (Section 2 of RULES.md): long-only, fractional, no backdating, proof by trade log.
2. One portfolio state file as the single source of truth, with a lifecycle gate that execution checks before acting.
3. Fills only at a session's official open or close, from one primary source with one fallback (never search-extracted prices, never mixed within a valuation) — and a fail-closed refusal to invent a price.
4. Scheduled evaluations that gather prices, news, and market data, then file a dated snapshot before reporting.
5. Structured reasoning over evidence — thesis, momentum, catalysts, risk, opportunity cost — with proposals written alongside the hold alternative.
6. The veto/approval gate: no execution job exists before the veto window closes or explicit approval is given.
7. Proceeds-funded, fractionally sized rebalances, recorded as paired fills with sources.
8. Risk metrics computed and reported at each cycle.
9. Read-only dashboard regenerated with current data at each cycle; controls only via chat, Discord, or CLI.
10. The write protocol: trade IDs, file lock, temp-file-and-rename, fixed write order, startup reconcile (Section 7.1).
11. The untrusted-input boundary: structured fact extraction, per-cycle trade cap, position cap, authenticated veto (Section 8.5).
12. Proposal expiry and pause semantics: no silent late fills; pause cancels open windows and pending jobs (Section 8.6).

Within this design, the non-negotiables are the gates, the audit trail, the primary/fallback price rule with source recorded per fill, the write protocol, and the untrusted-input boundary. Change the evaluation frequency or the dashboard look and the system still works; remove the approval gate, the fail-closed pricing rule, the per-fill source record, the reconcile step, the fact-extraction boundary, or the structured reasoning framework, and it is no longer the same system.
