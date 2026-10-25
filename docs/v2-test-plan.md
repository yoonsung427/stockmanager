# stockmanager v2 — test plan

**Companion:** `docs/v2-design.md`. Builds on existing harness: `tests/unit`, `tests/integration`, `tests/fixtures`, `tests/support`, and `sim/` doubles (calendar, channels, clock, faults, prices, reasoner, verifier). New doubles required: `sim/triage_model.py` (deterministic classifier stand-in), `sim/eventsdb.py` (in-memory SQLite stand-in), `sim/bridge.py` (confirm-token recorder).

Gate order matters: the Q1 benchmark (§4) gates A1's real model; everything else is deterministic and runs against doubles.

---

## 1. Conventions

- Every v2 requirement maps to at least one test; requirement IDs in test names/docstrings for traceability (`# S3`, `# C1`).
- Integration tests use the existing fault-injection doubles (outbox failure, clock skew, price failure); unit tests stay pure (no I/O) like `list_due_jobs`.
- Lint gate per C6: ruff + mypy --strict must be green before any v2 feature merges — repair the postmortem §6.2 baseline first as its own PR, not as a side effect of feature PRs.

---

## 2. Invariant gates (run in CI on every PR)

| Test | Proves |
|---|---|
| `test_bridge_never_executes_irreversible_without_confirm` | C1/D2: dispatch an irreversible verb through the bridge with no confirm token → handler not reached; with token → same handler, same result as typed command, audit row `llm_mediated=true`. |
| `test_bridge_read_verbs_pass_without_confirm` | STATUS/query/explanation through the bridge execute directly (no side-effect verbs), audit still records the sender kind. |
| `test_assistant_input_is_data_not_instruction` | C3: prompt-shape snapshot — assistant context renders quarantined as data block; a crafted instruction inside the content changes nothing in the bridge output. |
| `test_thread_failure_does_not_start_window` | C2/D4: thread creation fails, channel copy delivered → window starts; neither delivered → window stays closed. |
| `test_core_runs_commands_with_assistant_down` | C4/S5: assistant plane unavailable (sim fault), full command surface + buttons behave identically to v1. |
| `test_assistant_messages_route_through_outbox` | C5: assistant replies captured in outbox entries, per-channel order preserved, replay-after-outage works. |
| `test_clicker_not_in_allow_list_rejected` | H4: component click from non-allow-list user rejected through `authorize()` with identical reason text as a typed rejection. |
| `test_shared_handler_layer_rate_limit` | D2/R7: limiter lives at the handler layer; typed and assistant commands draw on one 10/min budget; CLI path is no longer exempt; overflow rejected identically. |
| `test_assistant_actor_distinct_in_audit` | R2: an assistant-confirmed irreversible action records `assistant:<uid>`, never `discord:<owner>`, in the hash chain; assistant cannot confirm its own proposal. |
| `test_button_click_while_bot_down` | H4: click during bot downtime — view timeout or queued replay defined and asserted; no silent drop. |
| `test_priority_lane_and_ttl` | alert kinds with `veto_window_open` deliver before narrative kinds during a rate-limit stall; TTL on undeliverable alerts defined and enforced. |

---

## 3. Requirement test plans

### Trust hygiene
- **H1** — fill in `sim/`, assert `dashboard/` files change at next render; the swallowed-`TypeError` path is gone; last-updated equals fill time.
- **H2** — `sim/calendar.py` gains holiday fixture from `data/calendars/nyse.json`; assert no fill scheduled on a known NYSE holiday, early-close handled, alert states actual fill date.
- **H3** — stale cache entry + live fetch fails → `stale=True` propagates through `PriceService`; both-source-failed path produces the informational alert, no fill, no window started. Fill-path refusal of stale=True asserted (R11).
- **H4** — embed component tests against `sim/channels.py`: click routes to handler identical to typed command; optional reason field stored in audit row (sender, ts, reason).
- **H5** — thread created on delivery (sim records `thread_id`); thread-reply resolves proposal ID implicitly; thread deleted mid-life → channel copy remains the record.
- **H6** — narrative rendering test: tick output through summary-layer fake produces catalogue-shaped message; silent ticks produce nothing.

### Attention (A1–A5)
- **A2 gate (hard):** `test_no_path_from_triage_to_execution_engine` — static + runtime assertion; triage module has no import or call edge to execution; wake goes only through the new `triage_escalation` DueJob kind and its dispatch branch (off-cycle run must not collide with the weekly slot or starve `deliver_pending`).
- **A1 gates:** hard-gate tests (held-ticker/event-kind enum, pure — raw-text pre-filter runs with no LLM call); confidence gate with `sim/triage_model.py` (deterministic fixed outputs): below threshold → silent, above → wake.
- **A3** — per R1, SET reachability is retracted: `SET triage.*` is refused, keys are file-edit only, strict defaults asserted at load; `SET ops.…`/`SET llm.…` still refused.
- **A4** — duplicate event hash inside window produces no second escalation; quiet-hours test defers items to the next morning digest (R5: nothing pages overnight; window starts at that morning delivery); noise ratio computed from `sim/eventsdb` fixture and asserted < 20% on the calibration set (§4).
- **A5** — every escalation row has a non-null source URL; no-citation input cannot escalate (negative test).

### Assistant plane (S1–S6)
- **S1** — explanation answers generated only from stored reasoner outputs + fact store fixtures (sim); an answer containing a claim with no matching stored fact fails; citation IDs resolve to stored records.
- **S2** — digest v2 joins a move fixture to an extracted-fact fixture; attribution line cites fact ID; embed shape (not message text) asserted; >1,900-char content allowed only via embeds.
- **S3** — full bridge flow test: free text → proposed command echo → no execution until confirm; confirmed path equals typed path byte-for-byte at the handler; rejection path identical to v1 for unresolvable intents.
- **S4** — constraint stored through bridge (with confirm), present in reasoner input next cycle, visible in STATUS, honored with assistant down (reads the prefs store via FileStore directly); enumerated-schema enforcement (free-text constraint entries rejected — no prompt-injection channel); MAX_VETOES window behavior unchanged, pattern-stats section capped and present.
- **S5** — dark-mode list asserted against sim fault: assistant features return unavailable, command layer unchanged.
- **S6** — assistant-marked messages distinguishable in outbox payload and audit rows.

### Outcomes (O1–O4)
- **O1** — executed trade fixture scored at 30/60/90 via `sim/prices.py` clock advance; win definition test (thesis-direction realized vs benchmark); no unscored trade while portfolio live. win per R8 relative-performance convention, trims scored on the trimmed fraction, 90-day horizon gates the hit-rate (30/60 trend only).
- **O2** — hit-rate computed from `sim/eventsdb`, appears in digest fixture and dashboard; executed-only rule enforced (vetoed proposals excluded from denominator); `system:evaluation` supersedes excluded from owner-veto stats.
- **O3** — reasoner input gains outcomes/stats section, size-capped, isolation rule inherited (snapshot test). Minimum-N gate test per R12: below 10 scored trades the section is provisional records only; stats enter input at N+10.
- **O4** — counterfactual narrative renders only from stored evaluation content; no fabricated claims (no external call in test).

### Metrics (M1–M5)
- M1 — `delivered` + `action` events for the same alert id in `sim/eventsdb`; median computation verified. Auto_approve fills excluded from the median (R13); auto-execution share reported separately.
- M2 — attention SLA computed from triage decision window fixtures.
- M3 — noise ratio report from fixtures (the < 20% target is asserted only against the §4 calibration set, not synthetic happy data).
- M4 — veto-rate trend + hit-rate trend rendered in weekly digest.
- M5 — guardrail report: every fill in fixture set traceable to deterministic verb, button, or timer + delivered alert; any LLM-executed irreversible action fails the test (this is C1 as a recurring report, run in CI).
- **SQLite availability fault:** event db write failure does not block delivery or fills (`sim/faults.py` case); core degrades to unavailable, outbox buffers. Triage dedupe key lives in the atomic-file store and survives the fault.
- **Classifier down (R4, fail closed):** no escalations produced while the classifier is unavailable; backlog re-runs on recovery without double-escalation (dedupe check before dispatch).

### Delivery & fault tolerance (post-review additions)
- **Thread filter:** `_is_allowed_channel` accepts thread replies; proposal delivery + thread creation in the same tick asserted against `sim/channels.py`.
- **Schema stability:** `Alert.from_json` on an old stored alert containing new fields must not crash the replay path.
- **Mainchat drain:** mainchat has a Hermes-backed sender wired through the outbox drain (R9); test asserts every configured channel has a live drain and the 4 live stuck pending alerts drain on first run after wiring.
- **Bot-process split:** bot process and scheduler process independently restartable; command handler runs in either.

---

## 4. Q1 benchmark protocol (gates A1 real model; run before implementation)

- **Corpus prerequisite:** the corpus does not exist yet — the pipeline does not persist raw fetched item text (only the reasoner input snapshot). Add a minimal fetch-log capture first; nothing can be labeled until it runs. Capture contract per R10/D8 (append-only JSONL in atomic-file store); a schema test asserts every row carries item_hash, source, url, raw text, fetched_at.
- **Shadow-mode run before production enable (R1 consequence):** classifier logs only (no escalation path) against live traffic for ≥4 weeks; human labels on live output. This gates real FP rates at actual base rates — a 100–200-item curated set is too small for a <20% noise target; the shadow log becomes the calibration corpus.
- **Measure on Strata/Hermes model:** p50/p95 latency per item, p95 batch against the independent triage-cron budget (target: p95 batch ≤ 60 s, per-item p95 ≤ 10 s).
- **Quality:** false-positive rate on the labeled/shadow set vs quiet-hours/noise budget (target FP ≤ 10% of material set; overall action-required ratio consistent with M3 < 20%).
- **Pass criteria:** both latency and FP targets met → Strata owns the classifier (D1). Fail either → fallback decision executes: hybrid hard rules + local pre-filter; classifier past the gate only.
- **Open before running:** the thesis-direction definition for SELL/trim win scoring (O1) is unresolved — outcome-scored hit rate cannot be computed until it's fixed.
- **Deliverable:** a checked-in results file (`docs/baseline-benchmark-v2.md`) with the corpus hash, labels, and numbers — reproducible rerun script under `tests/` marked as benchmark, not unit test.

---

## 5. Sequencing

1. C6 lint baseline repair (postmortem §6.2) — separate PR.
2. H1–H3 defect fixes + H4 buttons + H6 narrative + delivery-layer fixes (thread filter, mainchat drain, priority lanes, bot-process split) — P0, executable before the rest.
3. Invariant gates (section 2) land with the bridge, before any assistant feature.
4. Fetch-log capture → §4 benchmark → shadow-mode live run → A1 model decision, then triage.
5. Assistant plane features (S2, S1) after bridge gates green; outcomes (O1–O3) independent of the assistant plane; S4 last (needs prefs store).

---

*Plan only — no test code written, nothing committed.*
