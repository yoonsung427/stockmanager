# stockmanager v2 — design document

**Inputs:** `docs/v2-requirements.md` (incl. Section 8 decisions), `architecture-spec.md` (AS), `user-experience-spec.md` (UXS), `docs/project-postmortem.md`.
**Companion:** `docs/v2-test-plan.md`. Nothing here is implemented; this is the design as decided 2026-10-23.

---

## 1. Architecture overview

Two planes, one core:

- **Execution core (unchanged):** deterministic handlers (`commands/parser.py` + `commands/handlers.py`), proposal state chain, execution engine, derived scheduling (`scheduler/slots.py` + tick), atomic FileStore, outbox delivery, reconcile-halt. v2 adds only stages and namespaces; it does not move money through any new path.
- **Assistant plane (new, optional):** Hermes-based. Interprets free text, narrates, answers from stored content. It never calls handlers; it emits proposed commands to a bridge, and only deterministic confirmation reaches the handler.
- **Triage tier (new, its own cron job):** rule gates + classifier, run off the tick with a local raw-headline pre-filter. Output is an event log and, optionally, an off-cycle evaluation wake via a new `DueJob` kind — never a proposal, never a fill.

```
triage cron: raw pre-filter → classifier → triage log ──(silent | wake via new DueJob kind)
tick (unchanged shape): fetch → evaluation/execution/deliver_pending — triage latency must not starve this path
assistant plane (separate OS user): workspace snapshot read → narrative/answer/intent
intent → bridge → echo + confirm (bot-signed token) → deterministic handler (parser, auth unchanged)
```

---

## 2. Decision blocks

### D1 — Triage tier (A1, A2, A3, A5)
- New module `triage/`, run as **its own cron job** (post-review R7), not inline in the */5 tick: a slow triage stage starves `deliver_pending` and execution under the single-flight `tick_lock`, grace can be as short as 10 minutes, and the watchdog only checks between stages. Stages: local pre-filter on raw headline text (held ticker + regex/keyword event hints — no LLM call), then classifier, then decision log, then escalation. Event-kind extraction is itself an LLM call per item, so the original "hard gate before any LLM call" cost claim was wrong; the classifier runs only on items that pass the local gate.
- **Model:** Strata/Hermes per Q1 decision, gated by the benchmark in the test plan (p95 latency per item fits inside the */5 tick with margin; false-positive rate ≤ the noise budget). Fallback if the benchmark fails: hybrid hard rules + local pre-filter, classifier only past the gate.
- Config namespace `triage.*`: per R1, SET reachability is retracted — `triage.*` and `summary.*` remain **file-edit only** (`SET` never persists; dotted keys fail `extra="forbid"` validation). Keys: `triage.enabled`, `triage.confidence_threshold`, `triage.quiet_hours`, `triage.max_escalations_per_day`. Strict defaults (escalate only hard-gate AND confidence ≥ threshold).
- Escalation path: a triage "wake" needs a new `DueJob` kind (`triage_escalation`) with its own cycle-ID scheme and a dispatch branch in `tick.py` — `list_due_jobs` is a pure function with date-keyed slots, so an off-cycle run either never fires or suppresses the real weekly slot. Proposals enter the same veto window. No direct edge from `triage/` to `ExecutionEngine` (enforced by test, see test plan T-A2). Per R3, triage-originated proposals inherit `auto_approve` as-is; recorded consequence: with auto_approve on, news→fill can occur with no human action after the window — alert-before-fill remains guaranteed (window starts only on delivery).
- Every decision logged to the SQLite triage table (D7): item, gates, confidence, action, source URL. No citation → no escalation, enforced in code, not just policy.
- Dedupe key: `(ticker, event_hash)`, stored in the atomic-file store (not the SQLite table) so dedupe survives SQLite being down (R4). Failure direction: fail closed — no escalations while the classifier is down, backlog re-runs on recovery. Quiet hours (R5 ruling): all quiet-hours items defer to the morning digest, nothing pages overnight, and the veto window starts on the morning delivery for deferred items.

### D2 — Assistant bridge contract (S3, C1, Q2)
- Extend `Sender.kind` with `"assistant"` (currently `Literal["discord", "cli"]`). Assistant senders are no longer "trusted like CLI"; they pass the existing `authorize()` allow-list check as data, plus one added rule:
  - **Irreversible verbs** (C1 list): the assistant may only *propose* a command. The bridge emits the exact command text as a message/embed; execution happens only when the user confirms — a button click or typing the echoed command. The bridge then dispatches through `parser` + `authorize` unchanged.
  - **Read verbs** (`STATUS`, stock-query, explanation mode): the assistant may invoke handlers directly, since they have no side effects.
- Confirmation is not "the LLM asks nicely": the handler requires a confirm token that only exists after the echo was delivered and the user acted. Audit row records `llm_mediated=true` with the bridge's proposed text, the confirming user, and the final command. Per R2 the boundary is a real process boundary: the assistant runs as a separate OS user with a read/propose-only API, confirm tokens signed by the bot. Audit actor must record `assistant:<uid>` distinctly — `auth.py`/`_actor()` must change so assistant-confirmed actions don't hash-chain as `discord:<owner>`. Irreversible verbs move to a read-verb allow-list model, not a deny-list (PAUSE allowed instantly — it only reduces risk).
- Rate limit: assistant commands share a single budget with typed commands (actual limit is 10/min, in-memory, bot-only today; CLI bypasses). The limiter must move to the handler layer so both sender kinds draw on one budget; the assistant plane cannot self-authorize its way out.
- Auth stays: AS §11.3 allow-list is the boundary; one-person deployment keeps it as an invariant.

### D3 — Preferences store (S4, Q3)
- `workspace/preferences.json`, owned by stockmanager, written through the atomic FileStore. Hermes' `config.yaml` and memory are not part of the source of truth for constraints (C4).
- Structured entries: `{kind: "constraint", text, scope, set_at, source}`. Applied as a section in `input_builder` alongside the veto-history window (MAX_VETOES pattern preserved; stats added per O3).
- Per R1, SET reachability is retracted: prefs are written only through the confirm-gated bridge (or file edit); no `prefs.` prefix joins `SETTABLE_KEYS`. Secrets remain file/env-only per AS §2.5. Constraints use an enumerated schema (event kinds, tickers, severity), not free text — free-text prefs are a persistent prompt-injection channel the reasoner reads at every evaluation; the assistant may propose a constraint entry, the deterministic write is a separate gated call.
- The assistant plane may *write* entries via the bridge (echo + confirm, D2); the core *reads* them every cycle regardless of assistant availability.

### D4 — Threads and fallback (H5, Q4)
- Channel message remains the durable record; thread is a derivative created after delivery. Outbox entry gains an optional `thread_id`; thread-creation failure logs to the outbox as retryable work, message content already delivered. C2's veto-window rule is evaluated on the channel copy only.
- Interactive components (H4 buttons) live on the channel embed; a click resolves to the handler with `sender.kind="discord"`, user_id from the clicker. Non-allow-list clicks are rejected through the same `authorize()` path as typed commands.

### D5 — Outcome scoring (O1, O2, per Q5)
- New job `outcomes/`: on each tick, score executed trades at 30/60/90 days vs benchmark. Note (review): the benchmark mechanism does not exist yet — `SPY` is a config string with no return computation anywhere in the code; building benchmark-return scoring is a prerequisite for this block. Win = thesis-direction realized over horizon (thesis direction per ruling R8: relative-performance convention — for SELL/trim a win when the benchmark outperformed the traded fraction over the horizon; trims scored on the trimmed fraction; 90-day horizon gates hit-rate, 30/60 are trend reporting; computable from fills + prices alone, no LLM text).
- Hit-rate = wins / executed proposals; vetoed proposals excluded (decision Q5). Counterfactual scoring is out of scope for v2; only the O4 narrative remains (stored content, no scoring).
- Hit-rate + veto pattern stats join the reasoner input as a capped section (O3), same untrusted-content isolation. Minimum N gate per R12: hit-rate stats enter reasoner input only after 10 scored trades at the 90-day horizon; below N the section is provisional records only.

### D6 — SET surface (Q6, revised by R1)
- Per R1, `SETTABLE_KEYS` does **not** gain `triage.*` or `prefs.` — SET never persists (in-memory only; tick re-reads config.yaml each tick) and dotted keys fail validation. `BLOCKED_SET_PREFIXES` unchanged; no SET path reaches secrets. SET v2 scope: existing scalar keys only.

### D7 — SQLite for audit + metrics only (Q7, M1–M5)
- New `workspace/state/events.sqlite` (WAL, single writer, serialized under the existing store lock):
  - `audit_events(id, ts, sender_kind, user_id, command_text, channel, proposal_id, outcome, llm_mediated, confirm_ref)`
  - `metric_events(id, ts, kind, alert_id, proposal_id, value)` — kinds: `delivered`, `action`, `triage`, `outcome_score`
  - `triage_decisions(id, ts, ticker, event_hash, hard_gate, confidence, action, source_url)`
- SQLite is an **event log, never the source of truth**: proposal chain, books, and reconcile replay remain atomic-file. A corrupt/unavailable SQLite must not halt fills; audit and metric reads degrade to "unavailable," the core keeps running. The weekly C1 guardrail report queries this table; the guarantee it proves is code-enforced (the bridge rule), not LLM re-judged. Per review, the decision record itself (confirm → final action) is written into the proposal chain with an `assistant:<uid>` actor so audit survives SQLite being down; SQLite holds only replayable telemetry.
- M1 time-to-trust computation: `delivered` event ts → `action` event ts (button or command), same alert/proposal id, median across window.

### D8 — Fetch-log capture (benchmark prerequisite, R10)
- Append-only JSONL in the atomic-file store (`workspace/fetch-log/<cycle_id>.jsonl`), one row per raw fetched item (item_hash, source, url, raw_title, raw_snippet, fetched_at); 'no raw text to disk' rule scoped to logs/stats/rejected files, fetch-log is the named exception; read-only for labeling; retention >= shadow run + corpus-hash snapshot; must land before the shadow run starts.

---

## 3. Invariants as named gates (C1–C6 → tests)

| Invariant | Named gate | Where proven |
|---|---|---|
| C1 money-path determinism | `test_bridge_never_executes_irreversible_without_confirm` | test plan §2 |
| C2 nothing fills unseen | existing window tests + `test_thread_failure_does_not_start_window` | §2, §3 H5 |
| C3 untrusted-content isolation | `test_assistant_input_is_data_not_instruction` (prompt shape snapshot) | §2 |
| C4 degradation path | `test_core_runs_commands_with_assistant_down` | §3 S5 |
| C5 single delivery plane | `test_assistant_messages_route_through_outbox` | §3 S1/S2 |
| C6 test parity + lint gate | CI gate: ruff + mypy --strict green before v2 features merge (baseline repair first, postmortem §6.2) | CI |

---

## 4. Module change map

| Area | Change | Files |
|---|---|---|
| Triage | own cron job; pre-filter, classifier, decision log, DueJob wake | new `triage/`, `scheduler/tick.py` (dispatch branch), `scheduler.py` (`DueJob` kind + `SchedulerState` fields) |
| Config | no SET expansion (R1); `triage.py` config model file-edit only | `config/model.py`, new `triage.py` config model |
| Commands | `Sender.kind` extended to `"assistant"`; confirm-token rule; actor recorded as `assistant:<uid>`; limiter moved to handler layer | `commands/auth.py`, `commands/handlers.py` |
| Bridge | assistant→echo→confirm→dispatch, process boundary (separate OS user), bot-signed tokens | new `bridge/` (or `commands/bridge.py`) |
| Store | SQLite event log alongside FileStore; triage dedupe + decision records in atomic-file store | new `store/eventsdb.py` |
| Bot/Delivery | interaction handler + persistent views; `_is_allowed_channel` must accept thread replies; thread_id stored; `Alert.from_json` tolerates new fields; mainchat drain via Hermes-backed sender per R9 (outbox remains the single writer; C5 preserved); priority lanes + TTL for alerts | `bot/bot.py`, `adapters/discord_rest.py`, `notify/deliver.py`, `app.py` |
| Outbox | optional `thread_id`, interactive components on proposal embed | `notify/outbox.py`, `notify/deliver.py` |
| Outcomes | scoring job + hit-rate + reasoner section; benchmark-return machinery; veto-history stats must not count `system:evaluation` supersedes as owner vetoes | new `outcomes/`, `evaluation/input_builder.py` |
| Digest | v2 narrative + attribution join | `jobs/digest.py` |
| Calendar | holiday data consumed | `SimpleWeekdayCalendar` → `data/calendars/nyse.json` |
| Assistant plane | Hermes-side reader of `workspace/` snapshot + bridge CLI entry; publishes via outbox | Hermes skill/config, project CLI stays identical handlers |
| News/Fetch-log | raw item capture appended to atomic-file store per D8 (must land before benchmark) | news/pipeline.py, store/__init__.py |

Assistant-plane snapshot contract: read-only view of `workspace/` (proposal docs, extracted facts, preferences, event db) plus a read-only `STATUS`; assistant never writes workspace files directly except via the bridge.

---

## 5. Degradation matrix (C4)

| Feature | Assistant plane down |
|---|---|
| Command layer, buttons, veto/approve, fills | unaffected |
| Triage | fail closed (R4): no escalations while classifier down, backlog re-runs on recovery; dedupe survives via atomic-file store | unaffected — cron independent of assistant plane |
| Explanation mode, digest v2 personalization, conversational intents | dark (explicit dark-mode list in README) |
| Digest v1 shape, price alerts | unaffected |
| Metrics ingest | buffered in outbox-style retry or degraded to unavailable, never blocks delivery |

---

## 6. Non-functional budgets

- **Triage latency:** p95 per item ≤ 10 s within the */5 tick; per-tick triage batch ≤ 20 items, else defer to next tick. Locked by the Q1 benchmark before A1 merges.
- **Cost:** no per-item heavyweight call unless the hard gate fires; escalation-only classifier calls are budgeted separately from the weekly reasoner path.
- **Message shape:** assistant narrative via embeds (D7 note: embed content ≤ 4,000 chars; narrative truncation rule: cite-then-truncate, never truncate citations).

---

*Design decisions as recorded in the requirements doc Section 8; nothing implemented.*
