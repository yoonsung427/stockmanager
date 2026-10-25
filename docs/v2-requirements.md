# stockmanager v2 — requirements document

**Source:** `docs/llm-bot-autonomy-ux-analysis.md` (2026-10-23). Every requirement traces to a finding there (column T).
**Status:** requirements only — no design decisions made, no implementation. IDs are assigned; the v2 design phase resolves the open questions (Section 8).
**Priority:** M = must, S = should, C = could (MoSCoW). Wave = P0/P1/P2 from the analysis.

Repos conventions carry over unchanged unless a requirement overrides them: deterministic handlers, fail-closed pricing, derived scheduling, outbox at-least-once delivery, untrusted-content isolation, tests at parity with source.

---

## 1. Standing constraints (non-negotiable, inherited from v1)

- **C1 — Money-path determinism.** Every irreversible action (fill, veto, approve, close, `SET auto_approve`) is executed only by the deterministic command layer or a timer plus a delivered alert. No LLM output may directly execute an irreversible action. An LLM-mediated intent must be echoed as the exact deterministic command and confirmed separately (button click or the command itself), and logged as LLM-mediated.
- **C2 — Nothing fills unseen.** The veto window starts only after delivery to a visible channel (v1 rule; v2 surfaces may change, the rule may not).
- **C3 — Untrusted-content isolation.** Discord text and fetched news enter LLM contexts as quarantined data, never as instructions. The assistant plane inherits the `news/extract.py` isolation rule.
- **C4 — Degradation path.** If the assistant plane (or Hermes) is unavailable, the deterministic core and the project bot must still run commands identically. Assistant features may go dark; execution features must not.
- **C5 — Single delivery plane.** All user-visible messages, including assistant-generated ones, go through the existing outbox (ordering + replay-after-outage preserved). No direct Discord writes from the assistant plane.
- **C6 — Test parity.** New behaviors land with tests using the existing `sim/` doubles; lint gating (ruff + mypy --strict, currently failing per postmortem §6.2) becomes a pre-commit/CI gate before v2 features merge.

---

## 2. Trust hygiene (Wave P0)

| ID | Requirement | Acceptance criteria | P | Wave | T |
|---|---|---|---|---|---|
| H1 | The dashboard reflects fills at the next render. | A test that fills a proposal in `sim/` and asserts `dashboard/` files change; the swallowed-`TypeError` path removed; last-updated time matches the fill. | M | P0 | A5 |
| H2 | Fills are never scheduled on a non-trading day. | The calendar consumes `data/calendars/nyse.json` (holidays + early closes); alerts state the actual fill date; test with a known holiday. | M | P0 | A4 |
| H3 | Stale cached prices cannot bypass the fail-closed rule. | `prime()`/`get_price` stale entries raise or propagate `stale=True` through `PriceService`; both-failed path produces the existing informational alert, no fill. | M | P0 | A4 (postmortem 6.1(3)) |
| H4 | One highest-value change: proposal alerts carry tap actions. | Proposal embeds expose VETO/APPROVE interactive components; a click routes to the same handler as the typed command; audit record stores sender, timestamp, and the optional reason field; non-allow-list clicks rejected identically to typed rejections. | M | P0 | U2 |
| H5 | Each proposal gets a Discord thread; commands and replies live in it. | Thread created on proposal delivery; channel copy remains for cross-channel fallback (C2); thread replies carry the proposal ID implicitly (no `<id>` typing). | S | P0 | U3 |
| H6 | Tick/digest delivery is narrative, not raw stdout. | Hermes-delivered tick output is generated through the summary layer; message shape matches the existing catalogue severities; empty/silent ticks stay silent. | S | P0 | P0-4 |

---

## 3. Attention (event-driven triage, Wave P1)

| ID | Requirement | Acceptance criteria | P | Wave | T |
|---|---|---|---|---|---|
| A1 | The tick can classify materiality between evaluations. | A triage stage runs per tick over fetched items; two-gate rule: hard pre-filter (held ticker + event kind) AND classifier confidence ≥ threshold; below threshold → silent. Baseline change: attention window drops from ≤7 days to ≤N minutes (N set in design; target ≤30 min during market hours). | M | P1 | A1 |
| A2 | Triage may only wake the reasoner, never propose or trade. | An escalation triggers an off-cycle evaluation through the existing slot machinery; proposals still enter the same veto window; a test asserts no code path from triage to `ExecutionEngine`. | M | P1 | A1 |
| A3 | Triage thresholds are user-settable, strict by default. | Materiality gates join the `SETTABLE_KEYS` surface (or a new `triage.*` namespace with an explicit policy decision on SET reachability); default policy escalates only hard-rule + high-confidence. | S | P1 | U6, A1 |
| A4 | Dedupe and quiet hours. | A materiality decision is remembered per (ticker, event-hash); no duplicate escalation within the window; quiet hours suppress non-red alerts to the next morning digest. Noise guardrail: action-required alerts < 20% of total, measured. | S | P1 | U6 |
| A5 | Triage decisions are auditable. | Each triage decision (item, gates, confidence, action) written to a workspace log; every escalation cites the source URL that was fetched — no citation, no escalation. | M | P1 | A1, grounded-citations convention |

---

## 4. Assistant plane (Hermes, Wave P1–P2)

Architecture assumption inherited from the analysis (Option A, two planes): deterministic execution plane unchanged; assistant plane = Hermes with memory/search/skills, reading a workspace snapshot, publishing through the outbox. The interface shape is an open question (Q2).

| ID | Requirement | Acceptance criteria | P | Wave | T |
|---|---|---|---|---|---|
| S1 | Explanation mode. | User asks "why did you propose P-…?" or "what's your thesis on <held ticker>?" → answer generated only from stored reasoner outputs + extracted facts, each claim citing a stored fact/source ID; no new LLM judgment beyond stored content. | M | P1 | U4 |
| S2 | Digest v2. | Portfolio-level narrative, move→news attribution joined from `extracted-facts/`, personalization from veto history; delivered through outbox as an embed (not message text, >1,900 chars allowed via embeds). | M | P1 | U5, U7 |
| S3 | Conversational intents bridged to deterministic commands. | Free text like "swap NVDA for AMD" is interpreted, then echoed as the exact deterministic command + explicit confirmation step (H4 button or command echo); unconfirmed, nothing happens. Rejections identical to the v1 command layer. | M | P1 | U1, C1 |
| S4 | Durable preference constraints. | A stated standing constraint ("no energy exposure") is stored as structured data, applied every evaluation cycle, visible in `STATUS`, and honored without re-stating; veto pattern stats (not just last-N window) join reasoner input. | S | P2 | A3 |
| S5 | Assistant plane is optional. | With Hermes down, command layer + stock-query + digest still work as in v1; assistant features explicitly degrade (documented dark-mode list); no requirement depends on the assistant plane to protect money. | M | P1 | C4 |
| S6 | Assistant-generated messages carry a marker. | Every assistant-plane message is distinguishable from the deterministic core's messages in the UI and in the audit log. | S | P2 | U8, C1 |

---

## 5. Outcome and learning loop (Wave P1–P2)

| ID | Requirement | Acceptance criteria | P | Wave | T |
|---|---|---|---|---|---|
| O1 | Every executed trade is scored against its thesis at +30/+60/+90 days vs. benchmark. | Score persisted in the evaluation record; zero trades left unscored while the portfolio is live. | M | P1 | A2 |
| O2 | Proposal hit-rate computed and reported weekly. | Hit-rate visible in the weekly digest and dashboard; definition fixed in design (win = thesis-direction realized vs benchmark over horizon). | M | P1 | A2 |
| O3 | Hit-rate and veto patterns feed the reasoner input. | Reasoner input gains an outcomes/stats section (same isolation rule as veto history); a test asserts the section is present and capped in size. | S | P1 | A2, A3 |
| O4 | Counterfactual narrative. | "Considered but not proposed" content from stored evaluations rendered weekly as narrative with sources; no new claims beyond stored content. | C | P2 | A6 |

---

## 6. Metrics requirements (measured, not aspirational)

| ID | Requirement | Definition | P |
|---|---|---|---|
| M1 | North star: time-to-trust. | Median time a user acts on an alert without opening the dashboard first. Must be instrumented from the moment of delivery to the moment of action (command or button). | M |
| M2 | Attention SLA. | % of material events surfaced within the design-chosen window N minutes during market hours. | M |
| M3 | Noise ratio. | Action-required alerts / total alerts, target < 20%; reported in weekly digest. | S |
| M4 | Alignment proxy. | Veto rate per cycle trend vs. hit-rate trend, both reported. | S |
| M5 | Guardrail audit. | A recurring report proving C1: zero LLM-executed irreversible actions; every fill traceable to deterministic verb, button, or timer + delivered alert. | M |

---

## 7. Out of scope for v2 (explicitly)

- Real brokerage execution or any real-money path — mock fills stay.
- Multi-user portfolios, shared authority, or roles.
- LLM-initiated trading, continuous trading, or removing the veto window.
- Replacing the project bot entirely (analysis Option C rejected; S5 makes the assistant plane optional, not primary).
- Changing `start_cash` semantics (v1 rule stands).

---

## 8. Open questions the v2 design phase must answer

1. **Triage model choice (A1):** local model already wired (LM Studio `mistral-7b`) vs. Hermes' model — which owns the classifier, and what confidence threshold? Cost/latency budget per tick matters: a */5 tick cannot afford a heavyweight call per item.
	1. Answer: due to parallel 1, mistral was setup to avoid queueing. If we feel confident Hermes model can handle this and other loads. This needs to be tested now that we switched to a new LLM engine named Strata. We need to test this.
2. **Assistant-plane interface (S1–S3):** read-only status API, workspace snapshot JSON, or CLI-only access for Hermes? How are assistant intents handed to the command layer without bypassing auth (allow-list + rate limit)?
	1. Answer: 1. cli-onl access for hermes is where I'm leaning but open to understand RO status API etc. 2. i dont think we have auth. at the moment this is a 1 person (me) application.
3. **Preference storage shape (S4):** structured constraints vs. the existing reasoner input builder — does it live in `config.yaml`-equivalent user space, and is it SET-reachable or file-edit-only?
	1. Answer: lets setup it up for this stockmanager environment or workspace. can it have its own config.yaml vs. using the hermes config.yaml?
4. **Threads vs. channel messages (H5):** interaction with the cross-channel fallback (C2) — what happens when the thread is deleted or Discord components are unavailable?
	1. Answer: we need to be able to cache-queue so that we can fallback. if you can recommend an alternative fallback method - happy to discuss.
5. **Hit-rate definition (O2):** horizon, benchmark comparison, and how vetoed-proposal counterfactuals are handled (scored or excluded).
	1. Answer: i'm not sure. we need to spend time discussing this.
6. **SET surface policy (A3):** does triage join `SETTABLE_KEYS`, and what is the policy decision on which `SET` namespaces remain file-edit-only?
	1. Answer: is this in reference to secrets.env file that is used for this project. can we do something similar?
7. **LLM-mediated audit record (C1):** storage location and schema, and how the weekly report proves C1 without re-running the LLM.
	1. Answer: should we think of using a sqlite instnace?

### Decisions (confirmed 2026-10-23, post-answer)

- **Q1 → decided, gated by benchmark.** Strata/Hermes model owns the triage classifier, conditional on a benchmark passing first: latency per */5-tick item and false-positive rate against the M3 noise target (<20%). The benchmark run is itself a test-plan deliverable; if it fails, fall back to hybrid rules + local pre-filter.
- **Q2 → decided.** Assistant intents arrive as their own sender kind (`assistant`), not CLI passthrough. Irreversible verbs require echo + explicit confirm before the deterministic handler runs. Existing auth (Discord allow-list, AS §11.3) is retained as the audit boundary, not treated as a one-person convenience.
- **Q3 → decided.** Workspace-scoped stockmanager store (own preferences/config file), SET-reachable; Hermes' config.yaml untouched. Survives C4 (core reads it without the assistant plane).
- **Q4 → resolved by existing design.** The outbox is the cache-queue: channel message stays the durable record, thread is a derivative; thread-creation failure falls back through outbox retry, nothing is lost. One line in the design doc.
- **Q5 → decided.** Executed trades only: thesis direction realized vs benchmark at 30/60/90 days. Vetoed proposals excluded from hit-rate (counterfactual scoring deferred to O4 if ever added).
- **Q6 → decided.** `triage.*` gates join `SETTABLE_KEYS`, strict defaults; secrets stay in `~/.config/stockmanager/secrets.env` per AS §2.5 (SET never reaches secrets).
- **Q7 → decided.** SQLite for audit + metrics events only; proposal chain stays in the existing atomic-file store. Reconcile-halt semantics untouched; replay interaction is a design-doc section.

### Post-review revisions (2026-10-24, after Claude Code architecture review; full review at scratch/v2_review_sectionA.txt)

- **R1 — SET reachability retracted (supersedes Q3/Q6 rulings).** `SET` never persists (`handlers.py:437` in-memory only; tick re-reads config.yaml each tick) and dotted keys fail `extra="forbid"`. For v2, `triage.*`, `summary.*` and preferences are file-edit only; the SET-reachable claims in A3/D1/D3/D6 are dropped. `prefs` entries are written through the confirm-gated bridge, not SET.
- **R2 — C1 is a real process boundary.** Assistant runs as a separate OS user with a read/propose-only API; confirm tokens are signed by the bot. Audit must record `assistant:<uid>` as a distinct actor (fix `auth.py`/`_actor()` so the hash-chained record can't show `discord:<owner>` for an assistant-confirmed action). Irreversible verbs move to a read-verb allow-list model (PAUSE allowed instantly — it only reduces risk).
- **R3 — Triage authority: inherits `auto_approve` as-is.** User's explicit choice. Consequence recorded: with auto_approve on, news→proposal→fill can occur with no human action; alert-before-fill remains guaranteed (window starts only on delivery). This is compatible with C1 as worded (the LLM never *executes*), but it is a policy exposure and is flagged as such.
- **R4 — Classifier outage: fail-closed.** No escalations while the classifier is down; backlog re-runs on recovery. Dedupe must survive SQLite being down → triage dedupe moves from the SQLite table to the atomic-file store (small seen-set file).
- **R5 — Quiet hours: all items defer to morning.** Nothing pages overnight; the veto window starts on the morning digest delivery for deferred items. Red severity does not bypass quiet hours.
- **R6 — P0 rewrite.** H1, H2, and C6 are already satisfied in code (`JsonCalendar` wired at `app.py:190`, dashboard render matches, ruff/mypy green) — the analysis was built on the stale postmortem snapshot. Remaining real P0 defects: outbox `mainchat` stall (live: 4 stuck pending alerts, no `sent/`, dedupe-key retention, `[:100]` oldest-first starvation) + no priority lanes for `veto_window_open`; H3 stale-vs-fail-closed ruling still open; `Alert.from_json` must tolerate new fields before any outbox schema bump.
- **R7 — Fold-in defaults (not product calls):** fact-only digest attribution lines (LLM for tone only); enumerated-schema preference constraints instead of free text; triage runs as its own cron job with a local raw-headline pre-filter before any LLM call (event-kind extraction is itself an LLM call per item — the design's cost claim was wrong); shared rate limiter moves to the handler layer (actual limit is 10/min in-memory bot-only, CLI bypasses); Q1 benchmark runs in shadow mode (4 weeks live, label afterward) because a 200-item corpus has no statistical power at low base rate; benchmark-return machinery must be built — today `SPY` is a string with no scoring.
- Closed by ruling set 2 below: thesis-direction definition, mainchat, fetch-log capture, H3, minimum N, M1-under-auto_approve. Nothing remains open in this doc.

### Ruling set 2 (2026-10-24, PM decision pass)

- **R8 — Thesis-direction definition (O1/O2): relative-performance convention** (confirmed). Win = thesis direction realized vs benchmark at the 90-day horizon; direction inverts for SELL/trim: a win when the benchmark outperformed the traded fraction over the horizon. Trims scored on the trimmed fraction only. 30/60-day scores are trend reporting, not hit-rate gates. Must be computable from fills + prices alone: `thesis` is null for sells in the prompt spec and `stance`/`thesis_status` are not persisted domain fields, so scoring may not read LLM text. Vetoed proposals stay excluded (Q5).
- **R9 — Mainchat: keep, Hermes wired as its sender** (confirmed). Mainchat stays in `DEFAULT_CHANNELS`; its drain is the Hermes delivery path as a mainchat-backed sender. C5 preserved: the outbox remains the single writer for all user-visible messages — the Hermes sender is an adapter behind the existing outbox drain, not a second delivery plane. The 4 live stuck pending alerts drain through the wired sender on first run; no manual backfill.
- **R10 — Fetch-log capture contract** (confirmed). Append-only JSONL in the atomic-file store (`workspace/fetch-log/<cycle_id>.jsonl`), one row per fetched raw item: item_hash, source, url, raw_title, raw_snippet, fetched_at. The pipeline's 'no raw text to disk' rule is scoped to log lines, stats, and rejected files; the fetch-log is an explicit named exception, read-only for labeling/calibration, never re-ingested as instruction. Retention >= the shadow run plus a corpus-hash snapshot recorded in `docs/baseline-benchmark-v2.md`. Hard prerequisite: capture lands and runs before the shadow run starts — retroactive labeling is impossible.
- **R11 — H3 ruling (confirmed 2026-10-24): propagate the flag, fail closed at the money path.** Stale cache entries do not raise on read; `stale=True` propagates through `PriceService` (already implemented in code). The irreversible path (fill) refuses `stale=True` — fail closed. Both-source-failed path keeps the existing informational alert, no fill. Confirmed shape: self-recovery without a human step — stale fills are refused with an info alert only, and the fill path resumes automatically when a fresh price arrives on the next tick.
- **R12 — Minimum N before hit-rate influences the reasoner (confirmed 2026-10-24): N = 10 scored trades at the 90-day horizon.** Below N the outcomes/stats section renders as raw records marked provisional (no hit-rate inference in reasoner input); at N+ the stats enter input per O3. Rationale: same base-rate power argument as the R7/Q1 corpus ruling. Gate is self-activating: auto-flips at N when the 90-day horizon matures, no human gate.
- **R13 — M1 'acting' under auto_approve (confirmed 2026-10-24).** An auto-approve fill is not human acting: excluded from the time-to-trust median. M1 median is computed over alerts that required a human action (button or command); auto_approve traffic is reported separately as auto-execution share (% of alerts requiring no action). No conflict with M5 — C1 already covers timer + delivered alert.
- **R14 — Q1 benchmark measurement (confirmed 2026-10-25).** Latency targets are evaluated on observed runs: the 60s batch alert applies to p50 batch time at the actual prefilter-passed volume, not to a hypothetical 20 × p50. `max_items_per_run` stays 20; a full 20-item single-cycle batch is itself the signal of a prefilter pass-rate spike and alerts when observed. Probe evidence at decision time: p50 3.7s / p95 5.7s per item against the per-item ≤10s target (pass); worst-case-at-cap 73.7s. Adapter requirement added to the Q1 pass definition: the classifier adapter must handle strict-JSON answers delivered via `reasoning_content` when `content` is null (probe shape), or budget `max_tokens` above reasoning length — one of the two, proven in the benchmark rerun test. The FP half stays pending the ≥4-week labeled corpus; rerun script must record the corpus hash.
- **R15 — Assistant interface shape (confirmed 2026-10-25).** CLI-only read verbs: `status`, `explain`, `audit`. No HTTP status API: in the single-user context the workspace snapshot already serves read access and an HTTP surface adds injection surface without benefit. Intents remain bridge-mediated with echo + signed confirm tokens for irreversible verbs (R2 unchanged). Revisit only if a second consumer appears.
- **R16 — Durable-record rule for outbox threads (confirmed 2026-10-25).** The channel message is the durable record; thread content is derived, never primary. On thread-create failure the outbox posts the channel message without the thread link and retries the thread once; delivery is never gated on thread success. Component-unavailable fallback (buttons degrade to text commands) unchanged. No multi-tenant or per-user config exists in v2: single user, single channel, single writer (Q4 ruling stands; Q3 is resolved by the existing prefs module — SET never reaches production-enable flags or secrets).
- **R17 — Corpus dedupe: writer-side, first-occurrence (confirmed 2026-10-25).** D8 contract restated: one row per UNIQUE raw item per cycle; item_hash collisions within a cycle file are prevented at capture (first occurrence kept). The day-0 file (2026-10-25, 2102 rows, 980 duplicate rows from same-URL refetches across queries) is a frozen record excluded from the benchmark corpus: retained unmodified under fetch-log/excluded/, byte hash recorded in docs/baseline-benchmark-v2.md; span, hash pin and the Monday check measure from the first clean file.

---

## 9. Definition of done for the v2 design

The design is done when every open question above has a written decision, every M requirement has a test plan against the existing `sim/` doubles, C1–C6 are restated as invariants with a named test or gate, and the assistant-plane interface (Q2) is specified as a contract, not a vibe. The P0 items (H1–H3) can be executed independently of the v2 design — they are defect fixes, not design choices.

---

*Requirements only — no implementation, no changes to existing specs.*
