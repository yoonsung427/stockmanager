# Automated Portfolio System — User Experience Specification

**Companion to:** `automated-portfolio-system-spec.md` (the "system spec"). This document covers only what the user sees, types, and decides. Rules, limits, and internals live in the system spec and are referenced, not repeated.

**The experience in one line:** The AI runs the portfolio; you glance at Discord, check the dashboard when curious, and step in only when you disagree. Doing nothing is always safe — no real money moves, and every trade can be stopped before it happens.

---

## 1. Your three surfaces

| Surface | Use it to | Can it change anything? |
|---|---|---|
| **Discord** | Get alerts. Veto, approve, pause, resume. | Yes (allow-listed account only) |
| **Main chat / CLI** | Ask questions, change settings, close the portfolio. Same commands as Discord. | Yes |
| **Web dashboard** | See holdings, performance, risk, pending proposals. | **No.** Read-only; it shows the command to type when action is possible. |

Everything you change is a short text command, defined once here:

| Command | Effect |
|---|---|
| `STATUS` | Current state, pending proposals, next scheduled run |
| `APPROVE <id>` | Approve a proposal (or `APPROVE OPENING` for the first picks) |
| `VETO <id> [reason]` | Stop a proposal. The reason is optional but is remembered. |
| `PAUSE` / `RESUME` | Freeze or unfreeze all activity |
| `SET <setting> <value>` | Change a setting (Section 4) |
| `RECONFIRM <id>` | Ask for a fresh proposal to replace an expired one |
| `CLOSE` | Run the final valuation and end the portfolio |

`<id>` is the proposal ID shown in every alert (e.g., `P-2026-10-24-01`). Commands without a valid ID are ignored where an ID is required.

---

## 2. First-time setup

About 15 minutes, once.

| Step | You do | You see |
|---|---|---|
| 1. Choose a budget | Tell the agent your starting cash, or accept the $50,000 default. | Confirmation of `start_cash` |
| 2. Connect Discord | Add your Discord user ID to `config.yaml` (`discord_allowed_user_ids`). Add a Finnhub key for the price fallback. | Setup checklist with ✅ / ❌ per item |
| 3. Send a test alert | Type `STATUS` in Discord. | A blue "System ready" embed. If nothing arrives, the checklist says which item failed. |
| 4. Review opening picks | Read the staged picks: tickers, dollar targets, one-line thesis each. | Status **STAGED**. Nothing is invested. |
| 5. Adjust (optional) | Say "swap NVDA for AMD" or "cut the tech weight to 40%". | A revised pick list |
| 6. Approve | Type `APPROVE OPENING`. | Status **APPROVED**, with the time of the opening fills |
| 7. First fills | Nothing — it happens at the next market open. | Discord "Trade executed" per pick; status **LIVE**; dashboard opens in the preview pane |

**Getting started tip:** leave all defaults in place for the first month. Section 4 covers when to change them.

---

## 3. Day in the life

### 3.1 A normal week

| When (PT) | What happens | What you do |
|---|---|---|
| **Mon–Fri** | Nothing scheduled by default. | Nothing. Open the dashboard if you like. |
| **Sat 09:00** | Evaluation runs. | — |
| **Sat ~09:10** | Discord: **Evaluation summary** (blue). | Read it (30 seconds). |
| **Sat ~09:10** | If trades are proposed: Discord **Veto window open** (one message per proposal). | Decide: do nothing (it proceeds), `VETO`, or `PAUSE`. |
| **Sat ~13:10** | Veto window closes (default 4 h). | Nothing. |
| **Mon 06:30–06:40** | Approved trades fill at the open. | — |
| **Mon ~06:40** | Discord: **Trade executed** (green) per fill. | Glance. |

Weeks with no proposals are the common case: you get the summary and nothing else. "Hold" is the default.

### 3.2 What the Discord messages look like

| Message | Color | Contains | Needs action? |
|---|---|---|---|
| Evaluation summary | Blue | Value, ROI, vs. S&P 500, best/worst holding, proposed trades | No |
| Veto window open | Blue | Plain-language trade ("Sell 40% of AAPL, buy MSFT"), why, hold-alternative, veto deadline, the exact `VETO <id>` command | Optional |
| Trade executed | Green | Ticker, shares, fill price, new weight | No |
| Risk alert | Red | Which metric, current value vs. threshold, affected holdings | Your call; nothing is sold automatically |
| Earnings approaching | Blue | Ticker, date, current weight | No |
| System problem | Red | What failed, what the system did instead, what you can do (Section 5) | Sometimes |

Alerts never ask you to open a file or run anything not shown in the message.

### 3.3 The dashboard

Open the preview pane any time. It refreshes after each evaluation and shows a **last updated** time.

| Top of page | Middle | Bottom |
|---|---|---|
| Value, ROI, vs. S&P 500, drawdown, days running | Holdings table, performance chart, sector donut | Recent trades, earnings calendar, news, evaluation history |

**Status strip (always visible):** `LIVE` or `PAUSED`, current auto-approve mode, and any pending proposal with its deadline and the command to veto it. If the strip and Discord ever disagree, trust Discord (the dashboard may be a cycle old).

Click any evaluation in the history to read the full reasoning, sources, and ideas that were considered but not proposed.

---

## 4. Veto, approve, pause

### 4.1 The default: silence means yes

With `auto_approve: true`, a proposal runs unless you stop it before the veto deadline. With `auto_approve: false`, it runs only if you type `APPROVE <id>`.

| You want to… | Type | Result |
|---|---|---|
| Let it happen | Nothing | Fills at the next market open |
| Stop one trade | `VETO P-2026-10-24-01 too much tech already` | "Vetoed" confirmation; nothing fills; your reason is saved in the evaluation file |
| Approve early (approval mode) | `APPROVE P-2026-10-24-01` | "Approved" confirmation with the fill time |
| Stop everything | `PAUSE` | All open windows and pending fills are cancelled; Discord lists exactly which proposals were cancelled |
| Restart | `RESUME` | Status back to **LIVE**; cancelled proposals stay cancelled. The next evaluation proposes fresh. |

### 4.2 What you'll see after acting

- **Every command gets a reply** within seconds: ✅ accepted, or ❌ with the reason.
- Rejections you may hit: *"Unknown or expired proposal ID"*, *"Already executed"*, *"Sender not authorized"* (also posted as a red alert so you notice if someone else tries).
- A vetoed trade is final. If you change your mind, ask for a new idea; the next evaluation can propose it again with fresh prices.
- The agent learns from vetoes: repeated vetoes of a similar idea make it propose that idea less often. You'll see this noted in later evaluations.

### 4.3 Overriding the agent's judgement

You aren't limited to yes/no. Tell the agent what you want in plain language ("I don't want any energy exposure"). It will respond with an adjusted proposal that goes through the same veto window; it never fills straight from a chat request.

---

## 5. Changing settings mid-flight

Two ways, same result:

- **Chat or Discord:** `SET veto_window_hours 8`
- **File:** edit `config.yaml`, then type `STATUS` to confirm it was picked up.

The agent replies with *old value → new value* and *when it takes effect*.

| Setting you might change | Example | Takes effect |
|---|---|---|
| `auto_approve` | `SET auto_approve false` | Immediately. Open proposals switch to needing your `APPROVE`. Turning it back on never auto-approves anything already waiting. |
| `veto_window_hours` | `SET veto_window_hours 8` | Next proposal |
| `evaluation_frequency`, `evaluation_day`, `evaluation_time` | `SET evaluation_frequency daily` | Next scheduled run; the new schedule is shown in the reply |
| `max_position_pct`, `max_trades_per_cycle` | `SET max_position_pct 20` | Next proposal. Existing holdings are not sold to meet a lower cap. |
| Alert thresholds (`drawdown_alert`, `position_alert_pct`, `max_sector_pct`) | `SET drawdown_alert 8` | Next evaluation |
| `discord_allowed_user_ids` | Edit the file only | Next message |

Not changeable mid-flight: `start_cash` (it defines the return baseline). To change it, close and start a new portfolio.

**Rule of thumb:** a setting change never alters a trade you've already seen. It only shapes what happens next.

---

## 6. When something goes wrong

Every problem produces one red Discord alert in the same shape: **what happened → what the system did → what you can do**. In every case below the portfolio is left unchanged rather than guessed at.

| Alert | What it means | What to do |
|---|---|---|
| **Price source failure** | Primary price source failed; fallback used (or both failed). | If the fallback worked: nothing, it's informational. If both failed: nothing is traded this cycle. Wait for the next run, or type `STATUS` to see the retry time. |
| **Proposal expired** | A trade missed its fill window (outage, price failure, lock problem). Nothing was filled. | Do nothing to drop it, or `RECONFIRM <id>` for a fresh proposal that needs your explicit `APPROVE`. |
| **Run skipped: portfolio busy** | Another job held the file lock; the run stopped without writing. | Usually nothing; the next run succeeds. If it repeats, type `STATUS`; the agent will report whether a stale lock is blocking and offer to clear it. |
| **Books don't match (reconcile)** | Positions/cash disagree with the trade history. **All activity halts.** | Read the alert (which numbers differ). Reply `RECONCILE rebuild` to recompute positions and cash from the trade history, or ask the agent to explain first. Missing log lines are repaired automatically and just reported. |
| **Rejected control message** | Someone not on your allow-list, or a command with a bad ID, was ignored. | Check you used the right ID. If you don't recognize the sender, review `discord_allowed_user_ids`. |
| **Paused (you or a job reports it)** | Scheduled jobs ran but did nothing because the system is paused. | `RESUME` when ready. |

**If the agent itself seems stuck:** `STATUS` shows the last successful run and any halted job. `PAUSE` is always safe, and `RESUME` is always available.

---

## 7. Edge cases

The principle throughout: **a missed run is a skipped run, never a late trade.** You are told what was missed and given the next step.

| Situation | What happens | What you do |
|---|---|---|
| **Mac asleep at evaluation time** | On wake, the missed evaluation runs once, labeled "ran late," using the latest official closing prices. Alerts follow as normal. | Nothing. To avoid it, keep the Mac awake Saturday mornings. |
| **Mac asleep at fill time** | If it wakes before the proposal's expiry, the fill proceeds. Otherwise the proposal expires and you get the expiry alert. | Nothing, or `RECONFIRM <id>`. |
| **Mac asleep during a veto window** | The window keeps its real-time deadline. If it lapses while asleep, the trade proceeds only if the fill still happens inside its expiry; otherwise it expires. | If you're worried, `PAUSE` before closing the lid. |
| **Network down** | Pricing and news fetches fail; no valuation, no proposals, no fills. Alerts queue and arrive when the connection returns. | Nothing. |
| **Market holiday** | Fills move to the next trading day's open, and the expiry moves with them. The alert states the actual fill date. Evaluations use the last real close. | Nothing. |
| **Discord offline or bot down** | A veto window does not start until the proposal has been delivered to **at least one** channel you can see (Discord or main chat). If neither works, the proposal waits; nothing auto-executes unseen. | Veto, approve, and pause work from main chat/CLI. Check the dashboard status strip. |
| **Discord back after an outage** | Missed alerts arrive in order, with original timestamps. | Check for any "Veto window open" whose deadline hasn't passed. |
| **You're away for days** | Hold-only weeks need nothing. For proposals, you can set `auto_approve false` before leaving, so nothing executes unless you return and approve. | `SET auto_approve false` before you go; `SET auto_approve true` when back. |

---

## 8. Ending the portfolio

Type `CLOSE`. The agent confirms (*"This runs the final valuation and stops all scheduled runs. Type `CLOSE CONFIRM` to proceed."*), then posts a final report: ending value, total ROI, vs. S&P 500, best and worst holdings, full trade list. The dashboard shows **CLOSED** with the final numbers. All files stay in the workspace for reference.

---

## 9. Decisions this spec adds to the system spec

These UX behaviors go beyond what the system spec states and should be confirmed before implementation:

| # | Decision | Where |
|---|---|---|
| 1 | Command vocabulary (`STATUS`, `SET`, `RECONFIRM`, `RECONCILE rebuild`, `CLOSE CONFIRM`, `APPROVE OPENING`) | Section 1 |
| 2 | Every command gets an accept/reject reply | Section 4.2 |
| 3 | Setting changes follow the timing table (immediate vs. next proposal/run) | Section 5 |
| 4 | A missed evaluation runs once on wake, labeled "ran late" | Section 7 |
| 5 | A veto window starts only after the proposal reaches at least one visible channel | Section 7 |
| 6 | Alerts queue during network or Discord outages and replay in order | Section 7 |
| 7 | `start_cash` cannot be changed on a running portfolio | Section 5 |
