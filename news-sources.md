# News Sources — Data Pipeline Specification

Implements architecture-spec.md §7 (News pipeline; **AS §n** below). This document adds the concrete sources, configuration, fetch/dedupe rules, extraction call contract, error handling and integration points. It does not restate §7 where §7 is already precise; where this document goes beyond the specs it says so (see §9, "Additions and open questions"). Other companions: **SS §n** = automated-portfolio-system-spec.md, **LP §n** = llm-prompts.md, **TS** = testing-spec.md.

**Basis.** Reconciled against AS §3.6 (config), §3.8 (fact file), §3.9 (evaluation record), §9.2 (alerts), §10.3 (retry budget), §2.5 (secrets), SS §8.1/§8.5, and LP §3 (extractor prompt). Where this document extends one of those, the extension is tagged **⚑ext** and listed in §9; everything not tagged is already fixed by a companion spec.

**Hard constraints inherited from §7** (nothing below may weaken them):

1. Only search/feed/API endpoints are called. **Article pages are never fetched or followed.** The pipeline input is a headline and a snippet.
2. Everything left of the injection boundary handles untrusted text; everything right of it handles typed records only (AS §7.1, AS §7.3).
3. One LLM call per raw item, never batched (AS §7.4).
4. Provenance (`source_url`, `source_domain`, and the date when the fetcher supplies one) comes from the fetcher's record, never from the model.
5. If every query fails → `news_status: "unavailable"` → valuation and report proceed, **no proposals** (TS D14, EDGE-09).

---

## 1. Data sources

All sources implement the one `NewsFetcher` protocol from AS §7.2. Three adapters ship; they are ordered by a configurable fallback chain.

```python
class NewsFetcher(Protocol):
    name: str                                    # "searxng" | "finnhub" | "yahoo_rss"
    def fetch(self, query: NewsQuery) -> list[RawItem]: ...   # raises NewsUnavailable
```

| Priority | Adapter | Endpoint (no article-page fetches) | Role |
|---|---|---|---|
| 1 (default per AS §7.2) | `searxng` | `GET {base_url}/search?q=<q>&categories=news&format=json&time_range=<day\|week\|month>` on the Hermes search node | Primary for all query kinds. Keyword search, so it serves ticker, analyst-action, earnings, sector and macro queries uniformly. Requires `format=json` enabled in the instance's `settings.yml`. |
| 2 | `finnhub` | `GET https://finnhub.io/api/v1/company-news?symbol=<T>&from=<d>&to=<d>`; `GET /news?category=general` (macro); `GET /calendar/earnings?from=<d>&to=<d>&symbol=<T>` | Structured fallback. Cleaner per-ticker results. Free tier. Token goes in the `X-Finnhub-Token` header, never the URL (keeps it out of logs). |
| 3 | `yahoo_rss` | `GET https://feeds.finance.yahoo.com/rss/2.0/headline?s=<T>&region=US&lang=en-US` | Last-resort per-ticker fallback. Unofficial and undocumented: no SLA, may change or disappear. Treated as best-effort. |

Field mapping into `RawItem(title, snippet, url, published, domain)`:

| RawItem | searxng | finnhub | yahoo_rss |
|---|---|---|---|
| `title` | `title` | `headline` | `<title>` |
| `snippet` | `content` | `summary` | `<description>` (HTML stripped) |
| `url` | `url` | `url` | `<link>` |
| `published` | `publishedDate` → date, else `None` | `datetime` (unix, UTC) → date | `<pubDate>` → date |
| `domain` | host of `url` | host of `url`; see note | host of `url` |

Implementation notes:

- **`domain` is derived by the adapter from the URL it recorded**, lowercased, leading `www.` removed. The model never sees or supplies it.
- *Finnhub redirect URLs (verify against live responses):* Finnhub `url` values may point at a `finnhub.io` redirect rather than the publisher. If the host is `finnhub.io`, set `domain` from the response's `source` field instead, slugified (`^[a-z0-9.\-]{1,64}$`; otherwise `finnhub.io`). The URL is stored as given and **never followed**. Without this, the ≤ 3 per (ticker, domain) cap in AS §7.6 would collapse all Finnhub items into one bucket.
- **RSS/XML parsing uses `defusedxml`** (no external entities, no DTD). Never `xml.etree` on network data.
- **JSON/XML bodies are size-capped** (default 2 MB) before parsing, with connect timeout 5 s and read timeout 15 s. Redirects are followed only within the same adapter host and only up to 3 hops.
- **Finnhub call sharing.** `holding` and `analyst` queries for one ticker and window map to the *same* `company-news` request (the extractor classifies `rating_change`). The adapter memoises responses per `(endpoint, symbol, from, to)` for the cycle, so a holding costs 2 Finnhub calls (news + earnings calendar), not 3.
- **The Finnhub key and quota are shared with the price service** (`FINNHUB_API_KEY`, AS §2.5; `price_source_fallback: finnhub`, AS §6.2 limiter 30/min). The news budget in §2.2 is sized so both together stay under the vendor limit.
- **ETF coverage on Finnhub `company-news` is unverified.** If sector-ETF or benchmark queries return nothing on Finnhub, route those query kinds to `searxng` only (`kinds:` filter in config below).
- *Earnings calendar (Finnhub):* the calendar endpoint returns structured rows, not articles. The adapter synthesises one `RawItem` per row with a **template-built** title (`f"{symbol} earnings scheduled {date}"`, built only from a regex-validated symbol and a parsed date), empty snippet, `published=None`, and `url` = the calendar endpoint URL. Synthesised items pass through the same extractor and validator, so there is one path to a `Fact`. Because `published` is `None`, the model-proposed `date` (the earnings date) is used (§4.4).

---

## 2. Source configuration

### 2.1 Config block (operational: file-edit only, not reachable via `SET`; AS §3.6 "`news.*` — no")

AS §3.6 already defines `news.searxng_url`, `news.max_items_per_query`, `news.max_facts_per_ticker` and `llm.extractor_model`. Those keys keep their names and locations. Everything else below is **⚑ext** and must be added to the `Config` model (extra keys are an error under §3.6 validation) and to the §3.6 sample.

```yaml
llm:
  extractor_model: claude-haiku-5-5       # existing (AS §3.6); the extractor reads it from here

news:
  searxng_url: "http://<pi-host>:8080"    # existing (AS §3.6, §14.2 Q5)
  max_items_per_query: 10                 # existing; AS §7.2
  max_facts_per_ticker: 15                # existing; AS §7.6
  # --- ⚑ext below ---
  enabled: true
  fallback_chain: [searxng, finnhub, yahoo_rss]   # tried in order, per query
  lookback_days: 7             # fetch window for holding/sector queries; hard max 60 (= fact date floor, AS §7.5)
  macro_cadence_days: 7        # macro query runs when last success is >= this old (SS §8.1: weekly)
  earnings_horizon_days: 30    # per-holding earnings lookahead (AS §7.2, SS §8.1)
  max_items_per_cycle: 200     # AS §7.2
  max_facts_per_ticker_domain: 3   # AS §7.6
  max_facts_total: 150             # AS §7.6
  title_max_chars: 300         # AS §7.2, truncate not reject
  snippet_max_chars: 2000      # AS §7.2, truncate not reject
  stale_feed_days: 4           # §6.3 item 3
  fetch_stage_deadline_s: 150
  extract_stage_deadline_s: 300
  extractor:                   # model comes from llm.extractor_model
    concurrency: 8             # <= 8 (AS §7.4)
    timeout_s: 30
    max_retries: 2
    max_tokens: 600
  sources:
    searxng:                   # base URL is news.searxng_url
      max_concurrency: 4
      min_interval_ms: 100
      kinds: [holding, analyst, earnings, benchmark, sector, macro]
    finnhub:                   # key: FINNHUB_API_KEY from secrets.env (AS §2.5), never config.yaml
      rate_per_min: 25         # price fallback uses 30/min (AS §6.2); 30 + 25 = 55 < 60/min free tier
      max_concurrency: 2
      kinds: [holding, analyst, earnings, macro]
    yahoo_rss:
      rate_per_min: 30
      max_concurrency: 1
      kinds: [holding]
  circuit_breaker:
    failures_to_open: 3        # consecutive failures per adapter per cycle (AS §10.3 "3 consecutive failures per run")
    # an open breaker lasts for the remainder of the cycle only; resets next cycle
```

Validation at load (`doctor` runs the same checks): every name in `fallback_chain` has a `sources` entry (`searxng` is configured by `news.searxng_url`); `FINNHUB_API_KEY` is present in the loaded secrets if `finnhub` is in the chain (else that adapter is **disabled with a warning**, not a startup failure); `lookback_days` ≤ 60; `concurrency` ≤ 8; `max_items_per_cycle` ≥ `max_items_per_query`; every query kind has at least one adapter whose `kinds` covers it; `searxng_url` scheme is `http`/`https` and the host is not taken from any news-derived value (it is operator config only). An invalid `news` block follows the §3.6 rule: keep running on the last-good mirror, alert once per bad hash (`config_error`).

### 2.2 Rate limiting and retries

One token bucket per adapter, shared across all queries in the cycle (`news/ratelimit.py`). Per-request timeouts and retries follow the AS §10.3 row "News search": **connect 5 s / read 15 s, 1 retry**, applied to all three adapters (retry only on timeout, connection error, 5xx, 429; never on other 4xx).

| Adapter | Limit enforced | Source of limit |
|---|---|---|
| `finnhub` | `rate_per_min: 25` token bucket, burst 5. On HTTP 429: honour `Retry-After` if present, else 10 s; that is the one retry, after which the query counts as failed. | Vendor free tier (~60 calls/min, AS §6.2) minus the 30/min the price fallback may use. Confirm the current limit before relying on it. |
| `searxng` | `min_interval_ms` between requests and `max_concurrency`; self-hosted, so the limit is protective, not contractual. | Local policy |
| `yahoo_rss` | 30/min, concurrency 1. | Local policy (no published limit) |

A query needing a token waits up to the remaining `fetch_stage_deadline_s`; if the wait would exceed the deadline the query is **skipped for that adapter** (treated as failure → fallback to the next adapter), not queued indefinitely.

### 2.3 Fallback behaviour

Per query, adapters are tried in `fallback_chain` order, skipping adapters whose `kinds` don't include the query kind or whose breaker is open.

- **Fallback triggers on failure only** (`NewsUnavailable`: timeout, connection error, HTTP 5xx, HTTP 4xx other than 404-empty, 429 after retry, malformed body, rate-wait over deadline). **An empty result is a success** and does not fall through: a quiet holding is normal and probing three adapters for every quiet ticker would triple the call volume.
- Results are **not merged across adapters** for one query. The first adapter that succeeds owns that query. (Merging would raise the cross-source duplicate rate and the call count for marginal coverage.)
- A query is **failed** only when every eligible adapter failed. Failed queries are recorded in `uncovered_tickers` (§5).
- Circuit breaker: after `failures_to_open` consecutive hard failures from one adapter in a cycle, that adapter is skipped for the rest of the cycle (saves timeout latency). It is closed again at the start of the next cycle.

---

## 3. Fetch strategy

### 3.1 Query plan (`news/plan.py`)

```python
@dataclass(frozen=True)
class NewsQuery:
    kind: Literal["holding","analyst","earnings","benchmark","sector","macro"]
    ticker: str            # holding / ETF / benchmark ticker; macro uses the benchmark ticker
    window_from: date
    window_to: date
    text: str              # search string for keyword adapters, built from templates, never from news text
```

Per cycle:

| Kind | One per | Window | Search text (searxng) |
|---|---|---|---|
| `holding` | holding | `today − lookback_days` → today | `"{ticker} stock"` (+ company name from `portfolio.json` if present) |
| `analyst` | holding | same | `"{ticker} analyst upgrade OR downgrade OR price target"` |
| `earnings` | holding | today → `today + earnings_horizon_days` | `"{ticker} earnings date"`; Finnhub uses the calendar endpoint |
| `benchmark` | benchmark | `lookback_days` | `"{benchmark_name} market"` |
| `sector` | sector ETF | `lookback_days` | `"{sector} sector stocks"` |
| `macro` | cycle, only if last success ≥ `macro_cadence_days` ago | `macro_cadence_days` | `"stock market economy Federal Reserve"`; Finnhub: `/news?category=general` |

Query text is built only from the tickers, names and templates above. No news-derived text is ever fed back into a query (no "follow the lead" queries).

Plan order is the priority order used for budget allocation: holdings (analyst/earnings after the holding's own query), benchmark, sectors, macro.

### 3.2 Normalisation

Applied to every `RawItem` immediately after the adapter returns (`news/normalise.py`):

1. Truncate `title` to 300 and `snippet` to 2000 characters (AS §7.2: truncated, not rejected). Strip HTML tags and decode entities in `snippet` and `title` before truncating.
2. Reject (reason code) items whose URL is missing, not `http`/`https`, longer than 2048, or has userinfo. (AS §7.5 checks the same later; failing early avoids paying for extraction.)
3. **Recency window.** Drop if `published` is set and is older than `lookback_days` (`stale`), or more than 1 day in the future (`bad_date`; clock skew or tampering). `published=None` items are kept; their date is established at validation (§4.4). Earnings-kind items are exempt from the lookback floor (their window points forward).
4. **Dedupe.**
   - *By normalised URL (required by AS §7.2), global across all queries in the cycle:* lowercase scheme and host, drop leading `www.`, drop default ports and fragments, remove tracking parameters (`utm_*`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`, `cmpid`, `.tsrc`, `guccounter`), sort remaining parameters, strip a trailing slash except at root.
   - *By title key (addition, see §9):* NFKC + casefold, strip non-alphanumerics, collapse whitespace, SHA-1. Same title key across different domains is treated as syndication; keep the item with the earliest `published` (ties: higher-priority adapter).
   - When an item is returned by several queries, keep it once and record the first originating query in plan order; its ticker becomes `focus_ticker` in the extraction message (§4.2, `null` for benchmark/sector/macro queries). Other originating tickers are not sent; the relevance set still lets the model emit a record for any of them.
5. `item_id = sha256(normalised_url)`; used in logs, the rejected file and the extraction cache. It carries no news text.

### 3.3 Budget allocation

`max_items_per_query` (10) is applied per query, newest first. If the post-dedupe total exceeds `max_items_per_cycle` (200), items are taken **round-robin across queries in plan order**, newest first within each query, until 200. This prevents a single large holdings list or a noisy early query from starving later holdings, benchmark and sectors. Overflow is counted as `cap_items` in the rejected file.

### 3.4 Concurrency and deadline

Queries run on a thread pool bounded by the sum of the adapters' `max_concurrency`. The fetch stage has a wall-clock deadline (`fetch_stage_deadline_s`, default 90 s); queries not completed by then are marked failed (`fetch_deadline`) and appear in `uncovered_tickers`.

---

## 4. Extraction pipeline

### 4.1 Flow

```
RawItem (normalised, deduped, capped)
   ── INJECTION BOUNDARY ──
   → extract: 1 item = 1 LLM call (claude-haiku-5-5, concurrency ≤ 8)
   → candidates: list[Candidate]   (may be empty)
   → validate + sanitise (deterministic, AS §7.5)  → Fact | <cycle_id>.rejected.jsonl
   → rank and cap (AS §7.6)
   → persist extracted-facts/<cycle_id>.json (§3.8)
   → ReasonerInput (Fact.for_reasoner() projections only)
```

### 4.2 The call (`news/extract.py`)

Contract from AS §7.4 and LP §3, made concrete. **The prompt wording, user template and output schema live in LP §3 / `prompts/extractor/v1/` and are not restated here**; this section fixes how the news code calls them.

| Aspect | Setting |
|---|---|
| Model | `llm.extractor_model` (`claude-haiku-5-5`) |
| Tools / web | none |
| Prompt | `prompts/extractor/v1/{system.md, user.md.j2, output_schema.json}`, loaded with sha256 verification against `meta.yaml` (LP §2.2). **Integrity mismatch ⇒ the extractor is unavailable ⇒ `news_status: "unavailable"`** (checked once at stage start, before any fetch results are paid for). |
| Output | Structured outputs: `output_config.format = {type: "json_schema", schema: output_schema.json}`. **Not** forced `tool_choice` (rejected with a 400 by current models). |
| Parameters | `thinking: {"type": "disabled"}` (otherwise thinking tokens exhaust `max_tokens`), `output_config.effort: "low"`, default sampling (non-default `temperature`/`top_p`/`top_k` are 400s), no prefill, `max_tokens` 600, 30 s timeout, 2 retries (SDK: 408/409/429/5xx and timeouts only). Verify parameter names against the live API docs at implementation time (LP §3.1). |
| Context | One item only. No other items, no portfolio data, no prior facts. |
| Concurrency | `news.extractor.concurrency` ≤ 8; a failed or hostile item cannot affect another because there is no shared state between calls. |

**User message.** Rendered from `user.md.j2` (LP §3.3). The variables, and nothing else, are:

| Variable | Value | Trust |
|---|---|---|
| `cycle_date` | PT date from `Clock` | trusted |
| `benchmark` | `cfg.benchmark` | trusted |
| `allowed_tickers` | the cycle's relevance set (holdings ∪ benchmark ∪ sector ETFs ∪ configured universe) | trusted |
| `focus_ticker` | `ticker` of the **first query in plan order** that returned this item (§3.2 step 4), or `null` for benchmark/sector/macro queries | trusted |
| `published` | `RawItem.published` as `YYYY-MM-DD`, or `null` | fetcher-supplied |
| `title`, `snippet` | normalised, truncated text, passed through the `xe` filter (`&`, `<`, `>` escaped) | **untrusted** |

`url` and `domain` are **not** sent: the model has no use for them, they are attacker-controlled text, and provenance comes from the fetcher. `focus_ticker` is a hint only; the authoritative check is the relevance set in validation. (An earlier draft of this document used a JSON user message with `hint_tickers`; LP §3.3's tagged template with `xe` escaping supersedes it.)

**Response.** `{"candidates": [...]}` — an object, because structured outputs need an object at the top level. Each element is exactly four fields:

```python
class Candidate(BaseModel, extra="forbid"):
    ticker: str
    event: EventType        # closed enum
    description: str
    date: date
```

`EventType` is the closed set fixed by SS §8.5, AS §3.8 and LP §3.4: `earnings | guidance | rating_change | regulatory | product | m&a | macro | other`. The enum exists once, in `domain/`, and the JSON schema file is regenerated and diffed against it in CI (LP §3.7).

At most five candidates per item (LP §3.2 rule 4). A response with **more than five** is treated as a schema failure for the whole item (`extract_schema`) rather than truncated: an item that makes the model emit a flood is not one whose first five records deserve trust.

### 4.3 Per-item failure handling

Item-level failures produce **no facts and a reason-code line** in `<cycle_id>.rejected.jsonl`; they never abort the stage.

| Condition | Reason code |
|---|---|
| `stop_reason == "refusal"` | `extract_refusal` |
| `stop_reason == "max_tokens"` | `extract_max_tokens` |
| Output fails schema / `Candidate` parse, or more than 5 candidates | `extract_schema` |
| Timeout after retries | `extract_timeout` |
| Stage deadline reached before the item was processed | `extract_deadline` |

Infrastructure failures (connection errors, 5xx, 401/403, 429 after retries) are counted separately from item-level failures; they feed the extractor-down test in §6.2.

### 4.4 Validation and `Fact` construction (`news/validate.py`)

Exactly the AS §7.5 rules, applied to every candidate; **drop and log, never trim or repair** (the description length cut is the one sanctioned exception, per AS §7.5). Reason codes:

| Rule (AS §7.5) | Reason code |
|---|---|
| Shape: extra/missing field, `event` outside the closed set | `val_shape` |
| Ticker fails `^[A-Z][A-Z0-9.\-]{0,9}$` | `val_ticker_syntax` |
| Ticker not in relevance set (holdings ∪ benchmark ∪ sector ETFs ∪ configured universe). Macro items attach to the benchmark ticker. | `val_ticker_relevance` |
| Date invalid / > 90 days in the future | `val_date_future` |
| Date older than 60 days | `val_date_old` |
| Description empty after sanitisation | `val_description_empty` |
| Command lookalike (leading `VETO\|APPROVE\|PAUSE\|RESUME\|SET\|CLOSE\|RECONCILE\|RECONFIRM`, or contains "ignore previous" / "system:") | `val_command_lookalike` |
| URL scheme/length/userinfo (SS §8.5: a record without a source URL is dropped; the normalise step already rejects URL-less items, this is the second check) | `val_url` |

Sanitisation order for `description`: NFC → remove Unicode categories Cc and Cf → collapse whitespace to one line → cut to 200 chars → require non-empty. The command-lookalike test runs on the sanitised string (so zero-width splitting of "VETO" doesn't evade it).

`Fact` is exactly the AS §3.8 record; code assembles it:

| Field | Source |
|---|---|
| `fact_id` | `"f_" + sha256(ticker \| event \| date \| description \| source_url)[:12]` — deterministic, so replays and the cache produce the same IDs |
| `ticker`, `event`, `description` | validated candidate |
| `date` | **fetcher's `published` when the fetcher supplied one; otherwise the validated model-proposed date** (AS §7.3, literal reading; see §9 item 3 — this conflicts with LP §3.2 rule 7 and needs a decision) |
| `source_url`, `source_domain` | fetcher record only |
| `display_title` | the item's `title`, sanitised like `description`, ≤ 300 chars; dashboard display only, **excluded from `Fact.for_reasoner()`** |

No price field, no sentiment, no confidence (SS §6, TS INJ-06, LP §1.1). Per-item bookkeeping (`item_id`, `adapter`, `prompt_version`) is **not** stored on the `Fact`: `item_id` and `adapter` appear on rejected-file lines, and the prompt `{version, sha256}` is recorded under `llm.prompts` in the evaluation record (LP §2.2).

### 4.5 Ranking and caps (`news/rank.py`)

AS §7.6, in this order: sort newest first (tie-break `item_id` for determinism) → ≤ 3 per (ticker, domain) → ≤ 15 per ticker → ≤ 150 total. Drops are logged as `cap_ticker_domain`, `cap_ticker`, `cap_total`. Duplicate facts (same ticker, event, date, and identical sanitised description) are collapsed before capping.

### 4.6 Extraction cache (optional, cost control)

`workspace/cache/news-extract/<prompt_version>/<item_id>.json` stores the raw **candidate list** the model returned. A cache hit skips the LLM call, but **validation and caps always re-run** against the current relevance set, date window and rules. Key includes the title/snippet hash, so a changed snippet at the same URL misses. Entries expire after `lookback_days`. The cache stores model output only, never raw text. If caching is omitted, nothing else in this document changes.

---

## 5. Cycle output, status and persistence

```python
class NewsResult(BaseModel):
    cycle_id: str
    news_status: Literal["ok", "degraded", "unavailable"]
    facts: list[Fact]
    uncovered_tickers: list[str]      # queries that failed on every adapter
    stats: NewsStats                  # counts per stage, per adapter, per reason code
```

| `news_status` | Condition |
|---|---|
| `unavailable` | Every query failed (AS §7.2), **or** the extractor is down (§6.2). No proposals this cycle (TS D14, EDGE-09). |
| `degraded` | At least one query failed on every adapter, or an adapter's breaker opened, or extract infra-failure rate was non-zero but below the extractor-down threshold. Cycle proceeds. |
| `ok` | Otherwise. Zero facts with all queries succeeding is `ok` (a quiet week). |

Persisted per cycle, written and fsynced **before** reasoning (AS §7.6, §3.8, TS INJ-04):

- `extracted-facts/<cycle_id>.json` — the AS §3.8 document: `schema_version`, `cycle_id`, `extractor_model`, `fetched` (items after normalise/dedupe/cap), `extracted` (facts after ranking), `rejected` (dropped items and candidates), `facts[]`. **⚑ext:** add `news_status`, `uncovered_tickers` and a `stats` object (per stage, per adapter, per reason code) to that document.
- `extracted-facts/<cycle_id>.rejected.jsonl` — AS §3.8's sibling file: one line per dropped item or candidate, **per cycle**, not a global log:

```json
{"cycle_id":"2026-10-24","ts":"2026-10-24T09:03:11-07:00","stage":"validate","reason":"val_ticker_relevance","item_id":"<sha256>","adapter":"searxng"}
```

The evaluation record (AS §3.9) carries `facts_ref` and `news_status`; §3.9 currently enumerates `ok | unavailable`, so `degraded` is **⚑ext** — widen the enum there. If that is declined, store `degraded` as `ok` in the evaluation record and keep `uncovered_tickers` in `extracted-facts` as the only coverage signal.

No raw title, snippet or model output is written to the rejected file, logs, or alerts (reason code only, per AS §7.1). `RawItem.__repr__` stays redacted (AS §7.3). Raw items live in memory for the duration of the fetch→extract stages and are not persisted.

---

## 6. Error handling

### 6.1 Failure matrix

| Failure | Detection | Behaviour |
|---|---|---|
| Adapter timeout / connection error / 5xx | `NewsUnavailable` | Next adapter in chain; counts toward breaker. |
| HTTP 429 | status code | Wait per `Retry-After`/10 s, one retry; then failure → next adapter. |
| HTTP 401/403 (bad/missing key, blocked) | status code | No retry. Adapter disabled for the cycle (breaker opens at once) and alert raised (`news_source_auth`, ⚑ext, §6.4). |
| Malformed body (bad JSON/XML, wrong shape, oversize) | parser/size cap | Failure for that query; next adapter. Partial parse is not accepted: all-or-nothing per response. |
| Empty result, HTTP 200 | `len == 0` | **Success**, no fallback; the ticker simply has no news. |
| Individual malformed item inside a good response (missing title/url) | per-item check | Item skipped with `bad_item`; the rest of the response is used. |
| Every query fails | all queries in `uncovered` | `news_status: unavailable`; no proposals; valuation and report proceed; alert `news_unavailable` (AS §9.2). |
| Some queries fail on every adapter | `uncovered_tickers` non-empty | `degraded`; those tickers contribute no facts. Not an error for the cycle. |
| Fetch/extract stage deadline | wall clock | Unfinished work is counted as `fetch_deadline` / `extract_deadline`; stage ends with what it has. |
| Item-level extraction failure | §4.3 | No facts, reason code. |
| Extractor down | §6.2 | `unavailable`. |

### 6.2 "Extractor as a whole is down" (defined here; AS §7.4 states the rule without a threshold)

Count infrastructure failures only (connection errors, 5xx, 401/403, 429 after retries, timeouts), not refusals, schema failures or `max_tokens`. The extractor is down when, after at least 10 items attempted, **≥ 50 % of attempts ended in an infrastructure failure**, or when any 401/403 occurs (credentials broken). On trip, in-flight work is cancelled and the cycle gets `news_status: unavailable`. Below the threshold, failed items are simply rejected and status is `degraded`.

### 6.3 Stale data

There are three distinct meanings; each has a different treatment.

1. **Stale item** (older than `lookback_days` at fetch, or older than 60 days at validation): dropped. Normal.
2. **No recent news for a ticker:** not an error; the ticker has no facts and the Reasoner sees none. Absence of news is never inferred as "all clear" by the news layer; only `uncovered_tickers` and `news_status` carry coverage information.
3. **Suspected stale feed** (adapter returns 200 but is serving old content): after the `benchmark` query succeeds, if its newest `published` is older than `stale_feed_days` (default 4, configurable) the adapter is flagged `stale_suspected`, the query is re-tried on the next adapter, and a warning is logged. Applied to the benchmark query only because a liquid index should always have fresh headlines; applying it to small holdings would produce false positives.

**No carry-forward.** When a query fails, the previous cycle's facts for that ticker are **not** substituted. Stale news must not drive a trade.

### 6.4 Alerts (AS §9.2)

`news_unavailable` is in the AS §9.2 catalogue (severity info/alert, `dedupe_key` per kind + day, commands shown: `STATUS`) and is raised when `news_status == "unavailable"` for any reason: all queries failed, extractor down, or prompt integrity failure. The body names the cause class and no news text.

Two further kinds are **⚑ext** and need catalogue entries in `notify/templates.py` (with the AS §9.2 template test): `news_degraded` (once per cycle; adapter names and `uncovered_tickers`, no news text; severity info) and `news_source_auth` (credential failure; `dedupe_key` per adapter per day; severity alert). If the catalogue is not extended, both fold into the run-notes and `STATUS` output instead of Discord. All news alerts go through the durable outbox like every other alert.

---

## 7. Integration

### 7.1 Where the news stage sits in a cycle

1. Evaluation cycle starts; `cycle_id` allocated.
2. Price valuation runs independently of news (news never feeds `PriceService`, TS INJ-06).
3. News stage runs **outside the portfolio lock** (it is network- and LLM-bound; the lock is taken only in the commit phase, §8.2): plan → fetch → normalise → boundary → extract → validate → rank → persist.
4. `NewsResult` → `ReasonerInput` builder. If `news_status == "unavailable"`, the cycle skips the Reasoner for trading purposes: valuation and report only, **no proposals**.
5. Reasoner returns *intent* (§8.1 of the architecture); `ProposalBuilder` applies caps and gates under the lock.

### 7.2 Injection boundary: what crosses and what does not

Rules: SS §8.5 (untrusted input boundary) and AS §7.3. The reasoning step receives records of fixed shape and never the raw text; instruction-like content has no channel to reasoning; extraction output is never executed as a command; facts carry no prices.

| Crosses to the Reasoner | Never crosses |
|---|---|
| `Fact.for_reasoner()` per LP §4.4: `fact_id`, `ticker`, `event`, `date`, `description` (sanitised, ≤ 200 chars), `source_domain` | `title`, `snippet`, `display_title`, `source_url`, any model output not turned into a `Fact`, anything from the rejected file |

Concretely:

- `ReasonerInput` is `extra="forbid"` with no free-text news field (LP §4.4). **⚑ext:** to tell the Reasoner which holdings had no news *coverage* (as opposed to no news), add two typed fields: `news_status: Literal["ok","degraded"]` (the Reasoner is not called when `unavailable`) and `uncovered_tickers: list[TickerStr]` (same regex as AS §7.5). Both need the LP §4.4 field list and the reasoner template whitelist (LP §2.2) updated together. If declined, coverage gaps are visible only in the evaluation record and `STATUS`.
- `source_domain` is the only fetcher-derived string that crosses. Constrain it before it can enter a `Fact`: lowercase hostname matching `^[a-z0-9.\-]{1,253}$`, else the URL check fails (`val_url`).
- `news.raw` is importable only by `news.extract` and the dashboard display path; the `import-linter` contract (AS §1.8, contract 3) forbids `evaluation.reasoner_io` and the Reasoner adapter from importing it. News modules have no import path to `commands.handle` (TS INJ-05) or to `PriceService` (INJ-06).
- `description` is still model-written text derived from untrusted input: it is sanitised and screened, but the Reasoner prompt must continue to present facts as data, and the Reasoner remains bounded by caps and gates in code (AS §7.3 last row, TS INJ-08/09).
- Discord display of `display_title` (dashboard only) should send with mentions disabled so a headline containing `@everyone` cannot ping. *(Outside §7; flagged for the Discord section.)*

### 7.3 Per-cycle 10-trade cap

Rule: SS §8.5 — at most 10 fills per evaluation cycle (`max_trades_per_cycle`, default 10, SET range 1–10); a paired sell and buy counts as 2; extra ideas are listed in the evaluation file as not proposed. Defined in AS §7.7 and enforced in `domain/caps.py` by `ProposalBuilder` (AS §8.2 step 4), then **again at execution** (SS §8.5). News code does not implement or influence it; what news guarantees is that it cannot raise it.

Reference behaviour for tests (`apply_trade_cap`), matching AS §7.7:

```python
def apply_trade_cap(ideas_in_priority_order, max_trades_per_cycle):   # value read from config in the commit phase
    admitted, not_proposed, legs = [], [], 0
    for idea in ideas_in_priority_order:            # a paired sell+buy idea has idea.legs == 2
        if legs + idea.legs > max_trades_per_cycle:
            not_proposed = ideas_in_priority_order[len(admitted):]   # first overflow idea + everything after
            break
        admitted.append(idea); legs += idea.legs
    return admitted, [(i, "trade_cap") for i in not_proposed]        # -> ideas_not_proposed in the evaluation file
```

Rules to preserve: the count is **fills** (legs) across all proposals created this cycle; ideas are admitted whole (a pair is never split); once one idea overflows, nothing after it is admitted even if it would fit (AS §7.7: "and everything after it"); the cap value comes from `config` at commit time, never from Reasoner output, so a hostile "make 10 trades" is truncated (TS INJ-08).

News-specific consequences:

- Volume of news, number of facts, or the number of tickers in `uncovered_tickers` has no effect on the cap.
- `news_status: unavailable` → zero ideas are proposed; the cap is moot.
- The cap runs at AS §8.2 step 4, *before* the position cap (step 5). An admitted idea later resized to nothing by the position cap or cash check (`position_cap`) has already consumed its legs; later ideas are not back-filled into the freed slots. This is the literal reading; say so if back-filling is wanted.
- Truncated ideas are recorded with reason `trade_cap`; they are not carried over to the next cycle automatically (the next cycle re-reasons from fresh facts).

### 7.4 Module layout

```
news/
  plan.py          # NewsQuery, build_plan(portfolio, config, calendar)
  raw.py           # RawItem (redacted __repr__)
  fetchers/
    base.py        # NewsFetcher protocol, NewsUnavailable
    searxng.py  finnhub.py  yahoo_rss.py
  ratelimit.py     # token buckets, circuit breaker
  normalise.py     # truncate, strip HTML, recency, URL/title dedupe, round-robin cap
  extract.py       # per-item haiku call, Candidate model, extractor-down detector
  validate.py      # AS §7.5 rules -> Fact | RejectReason
  rank.py          # AS §7.6 caps
  pipeline.py      # run_news_stage(cycle) -> NewsResult
```

### 7.5 Test requirements

| Area | Test |
|---|---|
| Boundary | TS INJ-01: no ≥ 8-char run of any corpus headline appears in recorded Reasoner input. Add: `display_title` absent from `for_reasoner()`. |
| Isolation | TS IC-6 / INJ-10: extraction calls are one-per-item; a hostile item cannot change another item's output (assert N calls for N items). |
| Provenance | Model output containing a different URL/domain has no effect on `Fact.source_url`. |
| Validation | TS D16 corpus: homoglyph ticker, `ZZZZ` canary, 400-char ticker, `javascript:` URL, zero-width "V​ETO", future date +91 d, date −61 d, extra field `price`. |
| Fetch | TS D14 / EDGE-09: all adapters fail → `unavailable`, no proposals, valuation and report still produced. One adapter down → fallback used. Empty 200 → no fallback call. |
| Rate limit | Finnhub bucket never exceeds `rate_per_min` under a 40-holding plan (fake clock). |
| Dedupe | Same article with different `utm_*`/`www.`/fragment collapses; syndicated title across two domains collapses. |
| Budget | 25 holdings × 10 items → round-robin leaves every holding represented in the 200. |
| Cap | TS WP-11 / INJ-08: pair counts as 2; cap enforced again at execution; hostile intent list truncated. |
| Parsing | XML with external entity / billion-laughs payload is rejected (defusedxml). Oversize body rejected. |

---

## 8. Cost and timing envelope (planning numbers, not guarantees)

- Fetch: 3 queries per holding (news, analyst, earnings) + benchmark + sectors + occasional macro. For 20 holdings and 6 sectors that is ~68 searxng requests. On Finnhub (fallback only) the same plan is ~41 calls thanks to news/analyst sharing (§1), which at 25/min is ~100 s; that is why searxng is primary and why `fetch_stage_deadline_s` is 150 rather than the ~90 s that a searxng-only run needs. Expect to tune the deadline against the real search node. The §10.3 per-call budget (5 s connect, 15 s read, 1 retry) bounds any single query at roughly 40 s.
- Extraction: ≤ 200 haiku calls per cycle at concurrency 8. Typical call is a few seconds, so ~1 to 2 minutes; the worst case (30 s timeouts with 2 retries) is bounded by `extract_stage_deadline_s` (300 s) rather than by the per-call arithmetic.

---

## 9. Additions and open questions

Things this document decides beyond what the companion specs state, or leaves for confirmation. **⚑ext** items need an edit to the named spec before implementation; none contradicts a rule in AS §7.

1. **Sources.** AS §7.2 names only the SearXNG adapter as default and says "only the search API is called". `finnhub` and `yahoo_rss` are added as fallbacks, as requested; they call API/feed endpoints rather than a search API. "Article pages are never fetched" is kept as the binding constraint. Confirm this reading. Also related: SS §8.1 / AS §14.2 Q5 say earnings dates and analyst actions come from search text only; the Finnhub earnings calendar is an additional structured source for them.
2. **References.** The request's "§8.5 injection boundary" is **SS §8.5** (untrusted input boundary and trade limits). In the architecture spec the boundary is AS §7.3, the trade cap is AS §7.7, and AS §8.5 is the atomic-write `Transaction`. This document cites SS §8.5 for the rule and AS §7.3 / AS §7.7 for the mechanism.
3. **`Fact.date` semantics (needs a decision).** AS §7.3 says the fetcher's date overrides when supplied, while AS §7.5 validates a model-proposed date up to 90 days ahead (earnings), and LP §3.2 rule 7 instructs the model to return the *event* date (its own earnings example expects `2026-10-29` from an article published earlier). Taken literally, a headline published today announcing earnings on Nov 5 gets `date = today`, losing the event date; only calendar-synthesised items (published `None`) keep a forward event date. Recommendation: keep `date` as the validated event date from the model (fall back to `published`), and add the fetcher's `published` as a cross-check (event date more than 60 days before `published` is dropped). This amends AS §7.3; until decided, §4.4 implements the literal rule.
4. **⚑ext — `degraded` status** (AS §3.9 enumerates `ok | unavailable`), **`news_status`/`uncovered_tickers` in `ReasonerInput`** (LP §4.4), **`news_degraded` and `news_source_auth` alerts** (AS §9.2), **`news_status`/`uncovered_tickers`/`stats` in `extracted-facts`** (AS §3.8), and the new `news.*` config keys (AS §3.6). Fallbacks if declined are given at each point above. Whether buy ideas on `uncovered_tickers` should be blocked in code is a policy decision for AS §8.2, not decided here.
5. **Extractor-down threshold** (≥ 50 % infrastructure failures after ≥ 10 attempts), **`stale_feed_days`**, **title-key dedupe**, **round-robin budget**, **extraction cache**, **>5 candidates ⇒ reject item**: new in this document; AS §7 gives no numbers for them.
6. **Prompt/code split.** Event enum, prompt wording, schema file and user template are owned by LP §3 and AS §3.8; this document only consumes them. Editing prompt text means a new prompt version (LP §2.2).
7. **Vendor facts to verify before implementation:** Finnhub free-tier limit and whether `company-news` supports ETFs; whether Finnhub `url` values are publisher URLs or redirects; Yahoo RSS endpoint stability; SearXNG `format=json` enabled on the Hermes node (AS §14.2 Q5); Anthropic parameter names for `thinking` / `output_config` (LP §3.1).
8. **Data egress.** Headlines and snippets are sent to the Anthropic API for extraction (AS §14.2 Q13, default: acceptable for a mock portfolio).
9. **Discord display.** `display_title` shown on Discord should be sent with mentions disabled so a headline containing `@everyone` cannot ping. Outside AS §7; flag for AS §9.1.
