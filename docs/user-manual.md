# Stockmanager User Manual

Stockmanager is a semi-autonomous **mock** portfolio manager. It researches news, proposes trades, gives you a window to veto them, then simulates the fills against real session prices. **No real money moves and no brokerage is connected.**

This manual covers day-to-day operation. For design details, see `architecture-spec.md` and `automated-portfolio-system-spec.md`.

## Contents

1. [Getting Started](#1-getting-started)
2. [Discord Integration](#2-discord-integration)
3. [CLI Commands](#3-cli-commands)
4. [Evaluation Cycle](#4-evaluation-cycle)
5. [Dashboard](#5-dashboard)
6. [Portfolio Management](#6-portfolio-management)
7. [System Configuration](#7-system-configuration)
8. [Troubleshooting](#8-troubleshooting)
9. [Commands Reference](#9-commands-reference)

---

## 1. Getting Started

### What it does

Once a week (Saturday 09:00 PT by default) stockmanager:

1. Pulls news for every holding.
2. Asks an LLM to reason over the extracted facts and suggest changes.
3. Turns the suggestions into **proposals**, capped by your risk limits.
4. Posts each proposal to Discord and opens a **veto window** (4 hours by default).
5. If you do not veto, executes the trades as **mock fills** at the next session open (06:30 PT / 09:30 ET), using real prices.
6. Records every fill in `portfolio.json` and `trades.log`, then rebuilds the dashboard.

You stay in control: you can veto any proposal, pause everything, or (if you turn auto-approve off) require an explicit approval for each trade.

### Current deployment

| Item | Value |
|---|---|
| Status | LIVE |
| Positions | 78 |
| NAV | about $18M (start cash `17981444.7445`) |
| Evaluation | Weekly, Saturday 09:00 PT |
| Scheduler | `stockmanager tick` every 5 minutes, run by hermes cron |
| Notifications and control | Discord (one channel plus DMs from allowed users) |
| Benchmark | SPY |
| Prices | Yahoo Finance first, Finnhub as fallback |

### How the pieces fit

- **`tick`** is the heartbeat. Cron runs it every 5 minutes. Each tick works out what is due (evaluation, veto-window sweeps, executions, expiries) and does it. There are no per-trade scheduled jobs.
- **The bot** (`stockmanager bot`) is a long-running process that reads your Discord commands and delivers alerts. It is only a transport: if it dies, nothing is lost and the CLI works the same.
- **The CLI** (`stockmanager ...`) runs the same command handlers as Discord, so a command behaves identically on either surface.
- **The workspace** (`workspace/`) holds all state as plain files. No database.

### Where things live

| Path | Purpose |
|---|---|
| `workspace/portfolio.json` | Source of truth for cash, positions and trades |
| `workspace/trades.log` | Human-readable audit copy of every fill |
| `workspace/config.yaml` | Settings |
| `workspace/proposals/` | One JSON file per proposal (`P-YYYY-MM-DD-NN.json`) |
| `workspace/alerts/` | Outgoing notification queue |
| `workspace/dashboard/` | Generated dashboard (`portfolio.html`, `portfolio.json`) |
| `workspace/state/` | Bot cursor and heartbeat, scheduler heartbeat, tick lock |
| `workspace/nav_history/` | NAV time series |
| `workspace/extracted-facts/` | News facts extracted per cycle |
| `~/.config/stockmanager/secrets.env` | API keys (injected into `os.environ` at startup) |

### Running the CLI

Run from the project root so `workspace/` resolves, or point at a workspace explicitly:

```bash
stockmanager status
stockmanager --help
stockmanager status --workspace /path/to/workspace
export STOCKMANAGER_WORKSPACE=/path/to/workspace   # alternative to --workspace
```

`python -m stockmanager <command>` is equivalent.

Exit codes: `0` means the command ran, even if it answered "no such proposal". `1` means an operational failure (workspace missing, invalid config, unexpected error).

### Quick health check

```bash
stockmanager status
```

Sample output:

```
✅ STATUS: LIVE
cash: 123456.7890
positions: 78
lock: free
pending proposals:
  P-2026-10-24-01 state=VETO_WINDOW execute_at=2026-10-26T06:35:00-07:00
```

`LIVE` is normal. `PAUSED` means you (or a script) paused it. `lock: held by ...` while a job is running is normal; it should clear within minutes.

---

## 2. Discord Integration

### Who can talk to the bot

- Commands are accepted in the configured channel (`discord.channel_id`) and in DMs.
- Only users listed in `discord.allowed_user_ids` are obeyed. Everyone else gets `❌ Sender not authorized`.
- If `allowed_user_ids` is empty, Discord control is disabled entirely (the CLI still works).
- Bot accounts are ignored.

### Command rules

- The command word must be the **first word** of your message. `veto P-2026-10-24-01` works; "please veto P-2026-10-24-01" does nothing.
- Commands are case-insensitive: `status`, `Status` and `STATUS` are the same.
- Messages over 2000 characters are rejected.
- Rate limit: 10 commands per minute per user. Past that you get `❌ Rate limited: too many commands, try again in a minute.`
- Every command gets a reply starting with `✅` (it worked) or `❌` (it did not, with the reason).
- Replies never ping anyone, even if your veto reason contains an `@mention`.

### Discord commands

| Command | What it does |
|---|---|
| `STATUS` | Portfolio state, cash, position count, lock status, pending proposals |
| `VETO <id> [reason]` | Cancel a proposal before it executes |
| `APPROVE <id>` | Approve a proposal (only when it needs explicit approval) |
| `APPROVE OPENING` | Approve the staged opening picks of a new portfolio |
| `PAUSE` | Stop the system and cancel all open proposals |
| `RESUME` | Return a paused system to LIVE |
| `RECONFIRM <id>` | Re-propose an expired proposal |
| `RECONCILE [rebuild]` | Check that the books agree; `rebuild` repairs `trades.log` |
| `SET <key> <value>` | Change one setting |
| `CLOSE` then `CLOSE CONFIRM` | Final valuation and close of the portfolio |

Proposal IDs look like `P-2026-10-24-01` (date plus a two-digit sequence). Copy it from the alert.

### The veto and approval workflow

1. After an evaluation, the bot posts a **Veto window open** alert for each proposal, for example:

   > **Veto window open: P-2026-10-24-01**
   > The veto window for P-2026-10-24-01 closes in 4h, at 2026-10-24 13:05 PT.
   > Commands: `VETO P-2026-10-24-01 [reason]`, `PAUSE`

2. The deadline is computed **when the alert is actually delivered**, so if Discord was down and the alert went out late, you still get the full window from when you could see it.
3. To stop the trade, reply in the channel:

   ```
   VETO P-2026-10-24-01 too much exposure to semis already
   ```

   Reply: `✅ Vetoed P-2026-10-24-01.` The reason (up to 500 characters) is stored with the proposal.
4. If you do nothing and `auto_approve` is `true` (the current setting), the proposal is approved when the window closes and fills at the next session open.
5. If `auto_approve` is `false`, nothing executes until you send `APPROVE <id>`:

   ```
   APPROVE P-2026-10-24-01
   ```

   Approval must arrive before the proposal's execution time. After that you get `❌ Cannot APPROVE ...: its execution time has passed`.

Vetoes are judged by the **time you sent the message**, not when the bot processed it. A veto sent during the window is honoured even if the bot or the Mac was asleep and handled it later. A veto still works after the window closes, as long as the trade has not yet executed.

If you try to approve an auto-approved proposal you get `❌ ... is auto-approved; there is nothing to approve`. Veto is the only action available there.

### Notifications you will see

| Alert | Meaning | Commands offered |
|---|---|---|
| Evaluation complete | The weekly run finished (adds "(ran late)" if it started late) | none |
| Veto window open | A proposal is waiting; shows the deadline | `VETO <id> [reason]`, `PAUSE` |
| Trade executed | A mock fill was recorded, e.g. "BUY 120 NVDA for 15432.00 (mock fill, no real money moved)" | none |
| Risk alert | A risk metric crossed a threshold (drawdown, position size, sector) | `PAUSE` |
| Earnings approaching | A holding reports within 7 days | none |
| System paused / resumed | Confirms PAUSE/RESUME, lists cancelled proposals | `RESUME` after a pause |
| Proposal expired | The proposal was not executed in time (missed window, price failure, late delivery, or never approved) | `RECONFIRM <id>` |
| Proposal voided | Execution cancelled it (for example a cap would be breached); nothing filled | `STATUS` |
| Reconcile divergence | `portfolio.json` and `trades.log` disagree; the system halts | `RECONCILE rebuild`, `STATUS` |
| Rejected control message | Someone not allowed (or a malformed command) tried to control the bot | none |
| Price source failure | Yahoo or Finnhub failed; info if the fallback covered it, alert if both failed | `STATUS` |
| Run skipped | A job found the portfolio busy and skipped its slot | `STATUS` |
| System ready | First successful run, or `doctor` passing | none |
| Config error | `config.yaml` is invalid; running on the last known good copy | none |
| News unavailable | No news for the day, so no proposals were generated | `STATUS` |
| Bot offline | The bot heartbeat is stale | `STATUS` |
| Scheduler stalled | `tick` has not run recently | `STATUS` |
| Clock moved backwards | System clock jumped back; time-based jobs skipped this tick | `STATUS` |
| Calendar coverage | The session calendar has no data for a day | `STATUS` |
| Final report | Portfolio closed; ending value, ROI, benchmark comparison | none |

Alert wording can be customised in `ops.notification_catalogue` in `config.yaml` (file edit only). A broken override falls back to the default text, so a typo cannot silence an alert.

Rejected-command alerts are limited to one per sender per 10 minutes.

### Bot online / offline behaviour

**Online.** The bot:

- answers commands as they arrive,
- delivers queued alerts every 5 seconds,
- writes a heartbeat to `workspace/state/bot_heartbeat.json` every 30 seconds.

**Offline.** If the bot process or its Discord connection goes down:

- **Trading keeps working.** `tick` runs from cron independently of the bot. Evaluations, expiries and executions continue.
- **Alerts queue.** They wait in `workspace/alerts/` and go out when the bot is back, with the veto deadline recalculated from the actual delivery time. A proposal whose alert never reached you does not start its veto window, and if it cannot be delivered before its execution time it expires rather than executing unseen.
- **You use the CLI.** `stockmanager veto`, `approve`, `pause` etc. do exactly what the Discord commands do.
- **You get told.** If the heartbeat is stale for more than 5 minutes, `tick` raises a **Bot offline** alert (it appears once the bot is back or through any other visible channel).

**Coming back online.** On reconnect the bot reads the last-seen message from `workspace/state/bot_cursor.json` and processes any commands you sent while it was down, in order. Nothing is run twice. A veto sent while the bot was down is honoured as of when you sent it.

The bot needs the `DISCORD_BOT_TOKEN` (environment or secrets file) and `discord.channel_id` set. Start it with:

```bash
stockmanager bot
```

Run it under launchd or another supervisor so it restarts after a crash.

---

## 3. CLI Commands

Every command accepts `--workspace PATH` (default `$STOCKMANAGER_WORKSPACE` or `./workspace`). Most also accept `--config PATH` (default `<workspace>/config.yaml`).

The CLI is always trusted: no allow-list applies. Whoever can run the command on the machine can control the portfolio.

### `status`

Lock-free, read-only summary. Safe to run any time, even mid-evaluation.

```bash
stockmanager status
```

### `tick`

Runs one scheduler pass. Normally cron's job, but you can run it by hand, for example to nudge a stuck state.

```bash
stockmanager tick
```

Prints `tick: ok`, `tick: error`, or `tick: skipped (another tick is running)`. Exits `1` on error. Only one tick runs at a time; a second one exits immediately.

### `pause`

Stops the system. Every open proposal (waiting, in its veto window, or approved but not yet filled) is cancelled and cannot be revived.

```bash
stockmanager pause
# ✅ System paused. Cancelled: P-2026-10-24-01, P-2026-10-24-02.
```

Only works while LIVE (otherwise you get a ❌ explaining the current status).

### `resume`

Returns a PAUSED system to LIVE. Cancelled proposals stay cancelled; the next evaluation creates new ones.

```bash
stockmanager resume
# ✅ System resumed.
```

### `approve <proposal_id>`

Approves a proposal that needs explicit approval, before its execution time.

```bash
stockmanager approve P-2026-10-24-01
# ✅ Approved P-2026-10-24-01.
```

> The CLI `approve` takes a proposal ID. To approve staged opening picks of a fresh portfolio, send `APPROVE OPENING` through Discord, or `stockmanager approve OPENING`.

### `veto <proposal_id> [reason...]`

Vetoes a proposal. The reason is optional free text; no quotes needed.

```bash
stockmanager veto P-2026-10-24-01 earnings next week, wait
# ✅ Vetoed P-2026-10-24-01.
```

### `set <key> <value>`

Changes one setting. See [section 7](#7-system-configuration) for the allowed keys.

```bash
stockmanager set max_position_pct 25
# ✅ Set max_position_pct = 25

stockmanager set veto_window_hours 6
stockmanager set auto_approve false
```

### `news`

Runs the news stage once against the current portfolio, standalone. It does not create proposals or trades. Useful to check that news sources and the extraction step are working before Saturday.

```bash
stockmanager news
# news: status=ok facts=312 uncovered=none
```

`uncovered=` lists tickers with no usable news. Exit code `1` if news was entirely unavailable.

### `report`

Builds the dashboard and writes it to `workspace/dashboard/`. See [section 5](#5-dashboard).

```bash
stockmanager report
# wrote workspace/dashboard/portfolio.html
# wrote workspace/dashboard/portfolio.json
```

### `close [--confirm]`

Starts the final close of the portfolio. This is a two-step action.

```bash
stockmanager close
# ✅ Close requested. Send CLOSE CONFIRM within 10 minutes to run the final valuation and close.

stockmanager close --confirm
# ✅ Closed. The final report is recorded; positions are kept, not sold.
```

`--confirm` must come from the same kind of sender within 10 minutes. After closing, the portfolio is CLOSED and no further trading happens. Open proposals are cancelled. If prices cannot be fetched, nothing closes and you can retry.

### `doctor`

Checks that something is scheduled to run `tick` every 5 minutes or faster.

```bash
stockmanager doctor
stockmanager doctor --python /path/to/venv/bin/python
```

It inspects your user crontab and the launchd agent `~/Library/LaunchAgents/com.stockmanager.tick.plist`. Exit `0` if a valid entry is found, `1` otherwise.

> **hermes cron:** `doctor` cannot see jobs owned by hermes. On this deployment it will likely report that no scheduler entry was found even though the tick is running. Confirm the job with `hermes cron` and check `workspace/state/scheduler.json` (the `last_tick` time should be within the last few minutes).

### `init`

Creates a new workspace with a default config. Do not run this against the live workspace.

```bash
stockmanager init --workspace ./workspace-test --start-cash 50000
```

### `ack <line> [--note TEXT]`

Admin tool: acknowledges a specific `trades.log` line number so a known discrepancy stops being flagged.

```bash
stockmanager ack 142 --note "manual correction reviewed"
```

### `bot`

Runs the Discord bot (long-running). See [section 2](#bot-online--offline-behaviour).

### `help`

Prints the command list.

```bash
stockmanager help
```

### Commands available only on Discord

`RECONFIRM <id>` and `RECONCILE [rebuild]` are handled by the shared command layer but have no dedicated CLI subcommand. Run them from Discord.

---

## 4. Evaluation Cycle

### Schedule

Defaults: **weekly, Saturday, 09:00 PT** (`evaluation_frequency`, `evaluation_day`, `evaluation_time`). The evaluation is started by the first `tick` at or after that time. If the Mac was asleep or the tick was blocked, it runs late and the "Evaluation complete" alert says "(ran late)".

### What happens, step by step

1. **Gather news** for every holding. If news is entirely unavailable you get a *News unavailable* alert and **no proposals** are created that cycle.
2. **Extract facts** from the news. Prices quoted in news text are never used as prices.
3. **Reason**: the LLM reads the facts and the current portfolio, then suggests changes with a thesis for each.
4. **Build proposals**. The system, not the LLM, turns suggestions into trades and enforces your limits:
   - at most `max_trades_per_cycle` trades (3),
   - no position above `max_position_pct` (30%),
   - new positions no larger than 10% (`proposals.new_position_max_pct`),
   - no sector above `max_sector_pct` (80%).
5. **Notify**: an *Evaluation complete* alert, then a *Veto window open* alert per proposal.
6. **Wait out the veto window** (4 hours by default).
7. **Execute** at the next session open. Mock fills use real prices.
8. **Record and report**: books updated, *Trade executed* alerts sent, dashboard rebuilt.

### The veto window

- The clock starts only after the alert has actually been delivered to a channel you can see (`ops.visible_channels`, default `discord`). Nothing auto-executes unseen.
- Default length is 4 hours (`veto_window_hours`, 1 to 168).
- If the window ends after the scheduled fill time, the proposal expires instead of executing early.
- Fills happen at the next session open. A Saturday evaluation therefore fills Monday morning at 06:30 PT plus a few minutes (`ops.exec_delay_minutes`, 5).

### Proposal lifecycle

| State | Meaning |
|---|---|
| `PROPOSED` | Created, waiting for its alert to reach you |
| `VETO_WINDOW` | Alert delivered; the clock is running |
| `APPROVED` | Window closed with no veto (auto mode) or you approved it |
| `EXECUTED` | Filled and recorded |
| `VETOED` | You vetoed it |
| `EXPIRED` | Not executed in time; can be reconfirmed |
| `CANCELLED_PAUSED` | Cancelled by `PAUSE` |
| `CANCELLED_CLOSED` | Cancelled by the final close |
| `VOIDED` | Execution refused it (see below); nothing filled |

`VETOED`, `EXPIRED`, `CANCELLED_*`, `VOIDED` and `EXECUTED` are final.

### After you approve (or the window closes)

At the fill time the executor fetches prices from **one** source (Yahoo, or all-Finnhub if Yahoo failed for any ticker). It never mixes sources. It then re-checks your limits against actual prices. If the fills are good, it writes them to `portfolio.json` and `trades.log` and sends *Trade executed* alerts. The `price_source` is recorded on each fill.

If neither source can price everything, **the portfolio is left unchanged**. The proposal then waits for the next tick; if it cannot fill within `proposal_grace_minutes` (60) after its fill time, it expires. Nothing is ever filled late at a stale price.

### After you veto

The proposal goes to `VETOED` and never executes. Nothing else changes; the next scheduled evaluation proceeds as usual. Your reason is kept on the proposal and shown in its record. There is no "undo veto": if you change your mind, wait for the next evaluation, or reconfirm only if it later expires (a vetoed proposal cannot be reconfirmed).

### Expired proposals and RECONFIRM

A proposal expires if it was not executed in time: delivery too late, the fill window was missed, prices were unavailable, or (in explicit mode) you never approved it. The *Proposal expired* alert states the reason and offers:

```
RECONFIRM P-2026-10-24-01
```

This creates a **new** proposal (new ID) that:

- always requires your explicit `APPROVE`, regardless of `auto_approve`,
- is re-priced and re-checked on the next tick (limits and the thesis against the stored facts),
- is shrunk or voided, with an explanatory alert, if it no longer fits.

You can reconfirm an expired proposal only once.

### Voided proposals

Execution voids a proposal when it can no longer be done safely (for example a limit would now be breached). You get a *Proposal voided* alert with the reason. Nothing was filled, and RECONFIRM does not apply. Wait for the next evaluation.

### Pausing mid-cycle

`PAUSE` cancels every open proposal immediately. `RESUME` restores LIVE but does not bring back the cancelled ones.

### Switching auto-approve

- `SET auto_approve false`: every proposal still in `PROPOSED` or `VETO_WINDOW` becomes explicit. They will not execute without your `APPROVE`. Already-approved proposals are unaffected.
- `SET auto_approve true`: existing proposals stay explicit; only new ones use auto approval.

---

## 5. Dashboard

The dashboard is a **static, read-only HTML page** with no JavaScript and no controls. It cannot change anything.

### Generate it

```bash
stockmanager report
```

Output files in `workspace/dashboard/`:

| File | Content |
|---|---|
| `portfolio.html` | The page |
| `portfolio.json` | The same data in machine-readable form |
| `evaluations/eval-*.html` | One page per evaluation (reasoning, sources, ideas considered but not proposed) |

### View it

```bash
open workspace/dashboard/portfolio.html
```

### When it updates

You rarely need to run `report`. The dashboard is rebuilt automatically after every change to the books or proposals (evaluation commits, executions, veto, approve, pause). Run `report` manually if you edited the workspace by hand, or if the page looks stale. A dashboard failure never rolls back a trade; it is logged and the next change retries.

The page shows a **last updated** timestamp in PT at the top.

### Sections

| Section | Shows |
|---|---|
| Status strip | LIVE / PAUSED / CLOSED / STAGED / HALTED, whether auto-approve is on, and each pending proposal with ID, deadline, expiry and the literal command to copy |
| Summary | Value, ROI, benchmark-relative return, max drawdown, days active |
| Holdings | Ticker, thesis, shares, average cost, current price, value, return %, weight % (cash row included) |
| Performance | NAV vs benchmark, rebased to the same start; the data table is under a "details" toggle |
| Sectors | Sector weights (cash separate, "Unclassified" shown) |
| Risk | Max drawdown, sector concentration, largest position, volatility, Sharpe, correlation flags |
| Activity | Last 10 trades, newest first |
| Earnings | Holdings with earnings within 30 days |
| News | Per-holding headlines with links |
| Evaluations | Link to each evaluation page |

Metrics that cannot be computed yet (too little history) show as **n/a** rather than a misleading number.

---

## 6. Portfolio Management

### Lifecycle states

| Status | Meaning |
|---|---|
| `STAGED` | New portfolio with picks waiting for `APPROVE OPENING` |
| `APPROVED` | Opening approved; picks fill at the next open |
| `LIVE` | Normal operation |
| `PAUSED` | Manually paused; no proposals run |
| `CLOSED` | Final valuation recorded; no more trades |
| `HALTED` | The books disagree; trading stops until you reconcile |

### How positions are tracked

- `portfolio.json` is the single source of truth: cash, each position (ticker, shares, average cost, thesis) and the full trade history.
- `trades.log` is a human-readable audit copy, one line per fill. The two must agree; the system checks them before it writes anything.
- Money and share counts are exact decimals with 4 decimal places (no floating-point drift). Timestamps are PT.
- Every fill records its price source (`yahoo` or `finnhub`).
- All writes are atomic. A crash leaves either the old state or the new, never half of each.

### NAV

NAV = cash + sum(shares x price) across all positions, valued from a **single** price source per valuation. Yahoo Finance is tried first. If it fails for any ticker, the entire valuation is redone on Finnhub. If neither can price everything, nothing is updated; you see a *Price source failure* alert instead of a partial NAV.

The NAV history is stored in `workspace/nav_history/` and drives the dashboard's performance chart against the benchmark (SPY by default).

### Rebalancing

Rebalancing happens only through the evaluation cycle. There is no manual "buy" or "sell" command. The system proposes; you veto or approve. Limits that shape every proposal:

| Limit | Setting | Current value |
|---|---|---|
| Max trades per cycle | `max_trades_per_cycle` | 3 |
| Max single position | `max_position_pct` | 30% |
| Warn when a position exceeds | `position_alert_pct` | 40% |
| Max sector weight | `max_sector_pct` | 80% |
| Drawdown alert | `drawdown_alert` | 5% |

Positions are not sold automatically; every sell is a proposal you can veto.

### Reconciliation

If `portfolio.json` and `trades.log` disagree, the system **halts** rather than guess, and sends a *Reconcile divergence* alert. From Discord:

```
RECONCILE            # read-only check: "✅ Books reconcile cleanly." or "❌ Books diverge: ..."
RECONCILE rebuild    # rewrite trades.log from portfolio.json
```

`rebuild` trusts `portfolio.json` and regenerates the log. Use it only after you are satisfied that `portfolio.json` is correct. A missing log line caused by a crash is repaired automatically.

### Closing the portfolio

`CLOSE`, then `CLOSE CONFIRM` within 10 minutes (same sender). This takes a final valuation, cancels open proposals and records a final report with ending value, ROI and benchmark comparison. **Positions are kept, not sold.** The portfolio becomes CLOSED.

---

## 7. System Configuration

Config lives in `workspace/config.yaml`. The system validates it on load; if you save a broken file, it keeps running on the last good copy and sends a *Config error* alert.

### Keys you can change with SET

| Key | Allowed values | Current | Meaning |
|---|---|---|---|
| `auto_approve` | `true` / `false` | `true` | Approve automatically when the veto window closes |
| `veto_window_hours` | 1 to 168 | `4` | Length of the veto window |
| `evaluation_frequency` | `daily`, `weekly`, `biweekly` | `weekly` | How often to evaluate |
| `evaluation_day` | `monday` to `sunday` | `saturday` | Day for weekly/biweekly runs |
| `evaluation_time` | `HH:MM` (24h, PT) | `09:00` | Time of day |
| `max_position_pct` | 1 to 100 | `30` | Max weight of one position |
| `position_alert_pct` | 1 to 100 | `40` | Alert when a position drifts above this |
| `max_trades_per_cycle` | 1 to 10 | `3` | Trade cap per evaluation |
| `proposal_grace_minutes` | 10 to 240 | `60` | How long after the fill time a proposal may still execute |
| `max_sector_pct` | 1 to 100 | `80` | Max weight of one sector |
| `drawdown_alert` | above 0, up to 100 | `5` | Drawdown (%) that triggers a risk alert |
| `risk_free_rate_annual` | 0 to 0.2 | `0.0` | Used for Sharpe ratio |
| `price_source_primary` | `yahoo`, `finnhub` | `yahoo` | First choice of price source |
| `price_source_fallback` | `yahoo`, `finnhub` | `finnhub` | Used if the primary fails for any ticker |
| `start_cash` | positive number | n/a | Only while the portfolio is `STAGED` |

### Using SET

```
SET max_position_pct 25
SET veto_window_hours 6
SET evaluation_day friday
SET auto_approve false
```

CLI equivalent:

```bash
stockmanager set max_position_pct 25
```

Responses:

- `✅ Set max_position_pct = 25` on success.
- `❌ Invalid value for max_position_pct: ...` if the value is out of range or the wrong type. Nothing changes.
- `❌ <key> cannot be changed with SET` for any key not in the table.
- `❌ <key> is file-edit only and cannot be changed with SET` for keys under `ops.`, `llm.`, `news.` and `discord.`.

> **Check that it stuck.** After a `SET`, confirm the new value by looking at `workspace/config.yaml`. If the value is missing there, make the same change in the file by hand, since the file is what the next process start reads.

### Keys that need a file edit

| Key | Purpose |
|---|---|
| `benchmark` | Benchmark ticker (SPY) |
| `discord.channel_id` | Channel the bot listens in and posts to |
| `discord.allowed_user_ids` | List of Discord user IDs permitted to send commands |
| `ops.exec_delay_minutes` | Delay after session open before filling (5) |
| `ops.late_threshold_minutes` | Minutes after which a job counts as "ran late" (10) |
| `ops.lock_stale_minutes` | Age after which a dead process's lock can be broken (15) |
| `ops.visible_channels` | Channels that count as "you saw it" for starting a veto window (`discord`) |
| `ops.notification_catalogue` | Overrides for alert titles, bodies and severity |
| `news.max_items_per_query` | News items fetched per query (10) |
| `news.max_facts_per_ticker` | Facts kept per ticker (15) |
| `proposals.new_position_max_pct` | Max size of a brand-new position (10) |
| `llm.reasoner_model` | Model used for the reasoning step |
| `llm.reasoner_max_tokens`, `reasoner_effort`, `reasoner_timeout_s`, `reasoner_max_retries` | Reasoner tuning |

The `proposals:` block in `config.yaml` repeats some top-level keys (`max_position_pct`, `auto_approve`, `veto_window_hours`, ...). `SET` changes the top-level key. If you edit the file, keep the two copies consistent.

### Secrets

API keys and the Discord token are not stored in `config.yaml`. They come from environment variables or a secrets file:

**Secrets file location:** `~/.config/stockmanager/secrets.env`

On startup, `app.py` reads this file and injects each `KEY=VALUE` line into `os.environ` (only if not already set). Override the path with the `STOCKMANAGER_SECRETS` environment variable.

| Name | Needed for |
|---|---|
| `DISCORD_BOT_TOKEN` | Running the bot |
| `FINNHUB_API_KEY` | Fallback prices and some news |
| `ANTHROPIC_API_KEY` | The reasoning step |
| `SEARXNG_URL` | SearXNG news search (primary) |

---

## 8. Troubleshooting

Start with:

```bash
stockmanager status
stockmanager doctor
tail -n 50 workspace/trades.log
ls -lt workspace/state
```

### Commands

| Symptom | Cause and fix |
|---|---|
| `❌ Unrecognized command` | The first word is not a command. Check spelling and that the command is the first word |
| `❌ Sender not authorized` | Your Discord user ID is not in `discord.allowed_user_ids`. Add it to `config.yaml`. Or use the CLI |
| `❌ Discord control is disabled: no allowed_user_ids configured` | The allow-list is empty |
| `❌ Rate limited ...` | More than 10 commands in a minute. Wait 60 seconds |
| `❌ System busy` | Another job holds the portfolio lock (an evaluation or execution is running). Retry in a minute |
| `❌ VETO needs a valid proposal ID` | IDs look like `P-2026-10-24-01`. Copy from the alert or `STATUS` |
| `❌ No such proposal` | Wrong ID, or the file is not in `workspace/proposals/` |
| `❌ Cannot VETO ...: it is EXECUTED` | Too late; it already filled. A veto works until the fill is recorded |
| `❌ ... is auto-approved; there is nothing to approve` | You are in auto mode. Do nothing to execute; veto to stop |
| `❌ Cannot APPROVE ...: its execution time has passed` | Too late. Use `RECONFIRM` once it shows as expired |
| `❌ Message too long` | Over 2000 characters |

### The bot

**Bot does not respond in Discord.**

1. Is the process running? `ps aux | grep "stockmanager bot"`.
2. Check the heartbeat: `cat workspace/state/bot_heartbeat.json`. It should be less than a minute old.
3. Is `DISCORD_BOT_TOKEN` set? The bot prints `❌ DISCORD_BOT_TOKEN is not set` and exits if not. Same for a missing `discord.channel_id`.
4. In the Discord developer portal, confirm the **Message Content Intent** is enabled for the bot.
5. Restart it: `stockmanager bot`. Missed commands are processed on reconnect.
6. Meanwhile, use the CLI. Everything works without the bot.

**Bot offline alert.** The heartbeat has been stale for more than 5 minutes. Restart the bot. Queued alerts are delivered afterwards.

**Alerts not arriving but commands work.** Check `workspace/alerts/` for queued files. The deliverer runs every 5 seconds inside the bot; if the Discord REST call fails, it retries. Look at the bot's log output for "outbox delivery failed".

**Missed a veto window because the bot was offline.** A proposal's veto window starts only after its alert is delivered, so a bot outage cannot silently eat your window. If delivery could not happen before the fill time, the proposal expires and nothing executes.

### The scheduler

**Scheduler stalled alert, or `workspace/state/scheduler.json` has an old `last_tick`.**

1. `hermes cron` (list jobs). Confirm the 5-minute tick job exists and is enabled.
2. Run a tick by hand: `stockmanager tick`. If it prints `tick: ok`, the system works and the schedule is the problem.
3. `stockmanager doctor` checks crontab and launchd, but not hermes jobs.
4. Make sure the Mac is not asleep. A sleeping Mac does not tick; missed work is picked up when it wakes, and runs labelled "(ran late)".

**`tick: skipped (another tick is running)`.** Normal when two ticks overlap. If it persists for much longer than a normal tick, a process may be stuck. See "Stale lock" below.

### Prices and news

**Price source failure.** An info-level alert means Yahoo failed and Finnhub covered it: no action needed. An alert-level one means both failed: the portfolio was left unchanged. Check your connection and that the Finnhub key is valid. Pending executions retry on later ticks until the grace period ends.

**News unavailable.** No proposals were made that cycle. Run `stockmanager news` to see what failed. The next scheduled evaluation tries again.

### Locks

A lock file protects the books while a job writes. `STATUS` shows `lock: held by <job>`.

- Held for a few minutes: normal.
- Held much longer than `lock_stale_minutes` (15): the owning process probably died. The system breaks a lock automatically only when it is old **and** its owner process is gone.
- Never delete lock files by hand while a job might be running.

### Reconcile divergence / HALTED

1. Run `RECONCILE` (Discord) for the details.
2. Compare `workspace/portfolio.json` with `workspace/trades.log`.
3. If `portfolio.json` is right, run `RECONCILE rebuild`.
4. If `portfolio.json` is wrong, restore it from a backup before doing anything else. Backups are kept daily.
5. Run `RECONCILE` again; expect `✅ Books reconcile cleanly.`

### Config error

You saved an invalid `config.yaml`. The system keeps running on the last good copy. Fix the file (the alert includes a hash of the bad version) and it takes effect on the next load. The CLI prints `❌ ...` and exits `1` if it cannot load config at all.

### "workspace not found"

You ran the command from the wrong directory. `cd` to the project root or pass `--workspace`.

### Useful checks

| Question | Command |
|---|---|
| Is the system alive? | `stockmanager status` |
| Is a scheduler installed? | `stockmanager doctor`, `hermes cron` |
| Is news working? | `stockmanager news` |
| Is the dashboard current? | `stockmanager report` |
| Do the books agree? | `RECONCILE` (Discord) |
| Is the bot alive? | `cat workspace/state/bot_heartbeat.json` |
| Is the scheduler alive? | `cat workspace/state/scheduler.json` |

---

## 9. Commands Reference

### Command table

| Command | Discord | CLI | Arguments | Notes |
|---|---|---|---|---|
| Status | `STATUS` | `stockmanager status` | none | Read-only, lock-free |
| Veto | `VETO <id> [reason]` | `stockmanager veto <id> [reason...]` | proposal ID, optional reason (max 500 chars) | Works until the trade is filled |
| Approve | `APPROVE <id>` | `stockmanager approve <id>` | proposal ID | Explicit-mode proposals, before execution time |
| Approve opening | `APPROVE OPENING` | `stockmanager approve OPENING` | none | Only when status is `STAGED` |
| Pause | `PAUSE` | `stockmanager pause` | none | Cancels all open proposals |
| Resume | `RESUME` | `stockmanager resume` | none | Only when PAUSED |
| Reconfirm | `RECONFIRM <id>` | not available | expired proposal ID | Creates a new explicit-approval proposal |
| Reconcile | `RECONCILE` | not available | none | Read-only check |
| Reconcile rebuild | `RECONCILE rebuild` | not available | none | Rewrites `trades.log` from `portfolio.json` |
| Set | `SET <key> <value>` | `stockmanager set <key> <value>` | setting and value | See [section 7](#7-system-configuration) |
| Close | `CLOSE` | `stockmanager close` | none | Step 1 of 2 |
| Close confirm | `CLOSE CONFIRM` | `stockmanager close --confirm` | none | Within 10 minutes, same sender |

### CLI-only commands

| Command | Purpose |
|---|---|
| `stockmanager tick` | Run one scheduler pass (what cron calls every 5 minutes) |
| `stockmanager news` | Run the news stage once, standalone |
| `stockmanager report` | Build the dashboard in `workspace/dashboard/` |
| `stockmanager doctor [--python PATH]` | Check that cron or launchd is installed to run `tick` |
| `stockmanager bot` | Run the Discord bot (long-running) |
| `stockmanager init [--start-cash N]` | Create a new workspace |
| `stockmanager ack <line> [--note TEXT]` | Acknowledge a `trades.log` line |
| `stockmanager help` | List commands |

### Global CLI options

| Option | Meaning |
|---|---|
| `--workspace PATH` | Workspace directory (default `$STOCKMANAGER_WORKSPACE`, else `./workspace`) |
| `--config PATH` | Config file (default `<workspace>/config.yaml`) |

### Reply conventions

| Prefix | Meaning |
|---|---|
| `✅` | The command succeeded |
| `❌` | It was rejected; the text says why and nothing changed |

### Typical routines

**Saturday after the evaluation**

```
STATUS                         # see pending proposals and deadlines
VETO P-2026-10-24-02 reason    # stop any you disagree with
```

**Something looks wrong**

```
PAUSE                          # stop everything first
STATUS
RECONCILE
RESUME                         # when you are satisfied
```

**Bot is down**

```bash
stockmanager status
stockmanager veto P-2026-10-24-01 not comfortable
stockmanager bot               # restart it
```

**Tighten risk for next cycle**

```
SET max_position_pct 20
SET max_trades_per_cycle 2
```
