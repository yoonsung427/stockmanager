"""Daily digest (runs after the close on trading days): one outbox alert with a summary per notable holding.

A holding is *notable* if it moved more than 1% against the previous close or has recent news. The job also rolls
``state/last_close.json`` forward to today's closes, because the morning price check (``jobs.price_check``) compares
the open against it; that happens even when ``summary.digest_at_close`` is off.

The digest can carry a *narrative* section (v2 R6/R9/R10): short prose built only from stored extracted facts and
stored Reasoner outputs. Every claim cites the ids of the records it rests on and a claim that cannot be traced to a
stored record is rejected, never softened; no model is asked for a new judgment here. The section is size-capped
(``NARRATIVE_MAX_CHARS``) and only a claim count enters the structured alert fields. The digest stages its alert to
the outbox and lets the single writer deliver it: it never sends on a channel itself, and an alert created inside
quiet hours (R5) is held until they end (the morning delivery, which is also when veto windows start).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from stockmanager.config.model import Config
from stockmanager.domain.docs import Fact, ProposalDoc
from stockmanager.domain.ports import PriceProvider
from stockmanager.evaluation.reasoner_io import ReasonerOutput
from stockmanager.jobs.common import (
    LAST_CLOSE_NAME,
    NewsLookup,
    SummaryGen,
    close_prices,
    dump_last_close,
    et_date,
    holdings,
    load_last_close,
    move_pct,
    position_info,
    safe_news,
)
from stockmanager.llm.summary import SummaryError
from stockmanager.notify.deliver import Channel, deliver_pending
from stockmanager.notify.outbox import DEFAULT_CHANNELS, Alert, ChannelState, Severity, prepare
from stockmanager.notify.templates import digest_alert
from stockmanager.outcomes.models import describe, hit_rate
from stockmanager.outcomes.scoring import load_scores
from stockmanager.pricing.errors import PriceError, ValuationUnavailable
from stockmanager.reporting import (
    describe_noise,
    describe_trend,
    hit_rate_trend,
    noise_ratio,
    veto_rate_trend,
)
from stockmanager.store import FileStore
from stockmanager.store.lock import Clock
from stockmanager.triage.quiet import QuietHours

log = logging.getLogger(__name__)

NOTABLE_MOVE_PCT = Decimal(1)
FRIDAY = 4

#: Hard cap on the whole narrative section, header and citations included (R10). Claims past it are dropped whole.
NARRATIVE_MAX_CHARS = 1500
#: Cap on one claim's text; stored fact descriptions are model-written and may be long.
NARRATIVE_CLAIM_MAX_CHARS = 240
#: A fact explains a move only if it is dated within this many days before the digest date.
NARRATIVE_LOOKBACK_DAYS = 3
NARRATIVE_HEADER = "Narrative (each claim cites its source record):"


@dataclass(frozen=True)
class NarrativeClaim:
    """One sentence of narrative and the stored record ids (fact ids, evaluation ids) it rests on."""

    text: str
    source_ids: tuple[str, ...]
    ticker: str | None = None


@dataclass(frozen=True)
class Narrative:
    """The rendered, capped section. ``rejected`` counts unsourced claims, ``dropped`` those cut by the size cap."""

    text: str
    claims: int
    rejected: int
    dropped: int


def build_narrative(claims: Iterable[NarrativeClaim], known_ids: set[str]) -> Narrative:
    """Render ``claims`` in order, keeping only those whose every source id is a stored record id.

    A claim with no sources, an unknown source or empty text is rejected. Once the next claim would push the section
    past ``NARRATIVE_MAX_CHARS`` it and everything after it is dropped, so the order given is the priority order.
    The result is ``""`` when no claim survives.
    """
    lines: list[str] = []
    used = len(NARRATIVE_HEADER)
    rejected = dropped = 0
    full = False
    for claim in claims:
        text = " ".join(claim.text.split())
        if not text or not claim.source_ids or not all(i in known_ids for i in claim.source_ids):
            rejected += 1
            continue
        if len(text) > NARRATIVE_CLAIM_MAX_CHARS:
            text = text[: NARRATIVE_CLAIM_MAX_CHARS - 1].rstrip() + "…"
        line = f"- {text} [src: {', '.join(claim.source_ids)}]"
        if full or used + 1 + len(line) > NARRATIVE_MAX_CHARS:
            full = True
            dropped += 1
            continue
        lines.append(line)
        used += 1 + len(line)
    if not lines:
        return Narrative("", 0, rejected, dropped)
    return Narrative("\n".join([NARRATIVE_HEADER, *lines]), len(lines), rejected, dropped)


def move_claims(moves: Mapping[str, Decimal], facts: Iterable[Fact], today: date) -> list[NarrativeClaim]:
    """Attribute each notable move to stored facts for the same ticker dated within the lookback window (R6).

    The wording says the news coincided with the move, not that it caused it. A move with no such fact gets no claim.
    """
    out: list[NarrativeClaim] = []
    for fact in sorted(facts, key=lambda f: (f.date, f.fact_id), reverse=True):
        change = moves.get(fact.ticker)
        if change is None or not 0 <= (today - fact.date).days <= NARRATIVE_LOOKBACK_DAYS:
            continue
        text = f"{fact.ticker} moved {change:+.2f}%; same-window news ({fact.event}): {fact.description}"
        out.append(NarrativeClaim(text, (fact.fact_id,), fact.ticker))
    return out


def reasoner_claims(eval_id: str, output: ReasonerOutput) -> list[NarrativeClaim]:
    """One claim per holding assessment that carries evidence, from its enum fields only (no free model text, R10).

    The claim cites the evaluation record and the fact ids the Reasoner itself referenced.
    """
    return [
        NarrativeClaim(
            f"{h.ticker}: Reasoner stance {h.stance}, thesis {h.thesis_status}",
            (eval_id, *h.evidence_fact_ids),
            h.ticker,
        )
        for h in output.holdings
        if h.evidence_fact_ids
    ]


def veto_counts(proposals: Iterable[ProposalDoc]) -> dict[str, int]:
    """Vetoes per ticker from the hash-chained proposal files (the atomic-file truth, D7)."""
    counts: dict[str, int] = {}
    for doc in proposals:
        if doc.state != "VETOED":
            continue
        for ticker in {leg.ticker for leg in doc.legs}:
            counts[ticker] = counts.get(ticker, 0) + 1
    return counts


def personalise(
    claims: Sequence[NarrativeClaim],
    vetoes: Mapping[str, int],
    s4_join: Mapping[str, object] | None = None,
) -> list[NarrativeClaim]:
    """Order claims so tickers the owner has vetoed most come first (stable, so ties keep their input order).

    ``s4_join`` is reserved for the S4 join and is deliberately inert: it is accepted and never read.
    """
    del s4_join
    return sorted(claims, key=lambda c: -vetoes.get(c.ticker or "", 0))


def digest_release_at(now: datetime, quiet: QuietHours) -> datetime:
    """When a digest created at ``now`` may be delivered: ``now``, or the end of quiet hours (R5)."""
    return quiet.next_end(now)


def run_digest(
    store: FileStore,
    config: Config,
    summary_gen: SummaryGen,
    channels: Sequence[Channel],
    clock: Clock,
    *,
    price_provider: PriceProvider,
    price_fallback: PriceProvider | None = None,
    news_lookup: NewsLookup,
    reasoner_outputs: Mapping[str, ReasonerOutput] | None = None,
    s4_join: Mapping[str, object] | None = None,
) -> list[str]:
    """Build and enqueue today's digest; return the per-holding summaries it contains (``[]`` if none).

    ``price_provider``/``price_fallback`` and ``news_lookup`` are keyword-only additions to the bare job signature:
    the digest cannot judge a price move or a news hit without them. Closes come from one source (primary, else
    fallback). If neither can price everything, the digest still goes out judged on news alone and
    ``last_close.json`` is left as it was (fail closed). Raises ``SummaryError`` when every summary the digest needed
    failed, so the scheduler retries; ``LockBusy`` if the books are busy.

    ``reasoner_outputs`` maps stored evaluation ids to their stored outputs and feeds the narrative section along
    with the stored facts; ``s4_join`` is an inert placeholder. ``channels`` is only used for the immediate
    single-writer delivery pass after staging; the digest itself sends nothing.
    """
    if not store.portfolio_exists():
        return []
    portfolio = store.load_portfolio()
    if portfolio.status != "LIVE" or not portfolio.positions:
        return []
    held = holdings(portfolio)
    tickers = [h.ticker for h in held]
    today = et_date(clock())

    try:
        closes = close_prices(tickers, today, price_provider, price_fallback)
    except (PriceError, ValuationUnavailable) as e:
        log.warning("digest: no close prices for %s, judging on news alone: %s", today, e)
        closes = {}
    previous = load_last_close(store.root)

    summaries: list[str] = []
    moves: dict[str, Decimal] = {}
    wanted = failed = 0
    for h in held:
        change = move_pct(closes.get(h.ticker), previous.get(h.ticker))
        news = safe_news(news_lookup, h.ticker, today)
        if change is not None and abs(change) > NOTABLE_MOVE_PCT:
            moves[h.ticker] = change
        if not (change is not None and abs(change) > NOTABLE_MOVE_PCT) and not news:
            continue
        if not config.summary.digest_at_close:
            continue
        wanted += 1
        info = position_info(h, closes.get(h.ticker), today if h.ticker in closes else None, previous.get(h.ticker))
        try:
            text = summary_gen(h.ticker, news, info, today)
        except SummaryError as e:
            failed += 1
            log.warning("digest: no summary for %s: %s", h.ticker, e)
            continue
        summaries.append(f"**{h.ticker}** — {text}")
    if wanted and failed == wanted:
        raise SummaryError(f"digest: all {wanted} summaries failed")

    narrative = _narrative(store, config, moves, today, reasoner_outputs, s4_join)
    lines = [*summaries, *_weekly_outcomes_line(store, today), *_weekly_metric_lines(store, today)]
    alert = digest_alert(lines) if lines or narrative.text else None
    if alert is not None and narrative.text:
        alert.body = f"{alert.body}\n\n{narrative.text}" if lines else narrative.text
        alert.fields["narrative_claims"] = str(narrative.claims)
    with store.transaction("digest") as tx:
        tx.require(status="LIVE")
        if closes:
            tx.stage_state({LAST_CLOSE_NAME: dump_last_close(closes)})
        if alert is not None:
            now = clock()
            prepare(alert, now)
            release = digest_release_at(now, QuietHours.parse(config.triage.quiet_hours))
            if release > now:
                alert.channels = {n: ChannelState(next_attempt_at=release) for n in DEFAULT_CHANNELS}
            tx.stage_alerts({f"{alert.alert_id}.json": alert.to_json()})
    if alert is not None and channels:
        _deliver(store, channels, clock, config)
    return summaries


def _weekly_outcomes_line(store: FileStore, today: date) -> list[str]:
    """The hit-rate line (O2), once a week: on the Friday digest, when any trade has been scored."""
    if today.weekday() != FRIDAY:
        return []
    scores = load_scores(store.root)
    return [describe(hit_rate(scores))] if scores else []


def stored_alert_severities(root: Path, today: date) -> list[Severity]:
    """Severities of the stored alerts (pending, sent, dead) that occurred in the 7 days up to ``today``."""
    since = today - timedelta(days=7)
    out: list[Severity] = []
    for state in ("pending", "sent", "dead"):
        for path in sorted((root / "alerts" / state).glob("*.json")):
            try:
                alert = Alert.from_json(path.read_text(encoding="utf-8"))
            except (ValueError, KeyError):
                continue
            if alert.occurred_at is not None and since < alert.occurred_at.date() <= today:
                out.append(alert.severity)
    return out


def _weekly_metric_lines(store: FileStore, today: date) -> list[str]:
    """M3 noise ratio and M4 alignment proxy (veto-rate trend beside hit-rate trend), Friday digest only.

    Silent when nothing at all is stored (no alerts, proposals or scores): an all-"none yet" digest would be noise.
    """
    if today.weekday() != FRIDAY:
        return []
    noise = noise_ratio(stored_alert_severities(store.root, today))
    vetoes = veto_rate_trend(store.load_proposals())
    hits = hit_rate_trend(load_scores(store.root))
    if not (noise.total or vetoes.series or hits.series):
        return []
    return [
        describe_noise(noise),
        describe_trend("Veto-rate (by proposal created date)", "proposals", vetoes),
        describe_trend("Hit-rate (90d, by fill date)", "scored trades", hits),
    ]


def _deliver(store: FileStore, channels: Sequence[Channel], clock: Clock, config: Config) -> None:
    """Push the fresh alert out now rather than waiting for the next outbox pass; failures stay in the outbox.

    The single writer: delivery goes through ``notify.deliver_pending`` only — the digest never sends on a channel
    itself, and quiet-hours holds (``next_attempt_at``) are respected by the drain.
    """
    try:
        deliver_pending(store, channels, clock, time_budget=20.0, visible_channels=config.ops.visible_channels)
    except Exception:
        log.exception("digest: immediate delivery failed; the alert stays queued")


def _narrative(
    store: FileStore,
    config: Config,
    moves: Mapping[str, Decimal],
    today: date,
    reasoner_outputs: Mapping[str, ReasonerOutput] | None,
    s4_join: Mapping[str, object] | None,
) -> Narrative:
    """The sourced narrative from stored facts and stored Reasoner outputs; empty when digests at the close are off."""
    if not config.summary.digest_at_close:
        return Narrative("", 0, 0, 0)
    facts = store.load_facts()
    outputs = reasoner_outputs or {}
    claims = move_claims(moves, facts, today)
    for eval_id, output in sorted(outputs.items()):
        claims.extend(reasoner_claims(eval_id, output))
    known = {f.fact_id for f in facts} | set(outputs)
    narrative = build_narrative(personalise(claims, veto_counts(store.load_proposals()), s4_join), known)
    if narrative.rejected or narrative.dropped:
        log.warning(
            "digest narrative: %d claims rejected as unsourced, %d dropped by the cap",
            narrative.rejected,
            narrative.dropped,
        )
    return narrative
