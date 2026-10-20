"""Normalise fetched items (news-sources §3.2, §3.3): clean text, check URL and date, dedupe, cap.

Runs left of the injection boundary on untrusted text; its output feeds ``extract``. Every drop is a ``Rejection``
with a reason code and the item's id, never its text.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from stockmanager.news import Rejection
from stockmanager.news.config import NewsConfig, QueryKind
from stockmanager.news.plan import NewsQuery
from stockmanager.news.raw import RawItem, clip, strip_html, url_acceptable

_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "cmpid", ".tsrc", "guccounter"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_NO_FOCUS: frozenset[QueryKind] = frozenset({"benchmark", "sector", "macro"})


@dataclass(frozen=True)
class QueryResult:
    """What the winning adapter returned for one query."""

    index: int  # position in the plan
    query: NewsQuery
    adapter: str
    items: Sequence[RawItem]


@dataclass(frozen=True)
class PreparedItem:
    """A cleaned, deduped item ready for extraction. ``item_id`` is ``sha256(normalised_url)``: no news text."""

    item: RawItem
    item_id: str
    query_index: int
    focus_ticker: str | None  # first query (plan order) that returned it; None for benchmark/sector/macro
    adapter: str


@dataclass(frozen=True)
class Normalised:
    """Output of ``normalise``."""

    items: list[PreparedItem]
    rejections: list[Rejection]


@dataclass
class _Entry:
    prepared: PreparedItem
    title_key: str
    seq: int


def normalise_url(url: str) -> str:
    """Canonical form for dedupe: lower-case scheme and host, no ``www.``, default port, fragment, tracking params."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().removeprefix("www.")
    if ":" in host:
        host = f"[{host}]"
    netloc = host if parts.port in (None, _DEFAULT_PORTS.get(scheme)) else f"{host}:{parts.port}"
    params = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    )
    path = parts.path.rstrip("/") if parts.path != "/" else "/"
    return urlunsplit((scheme, netloc, path, urlencode(params), ""))


def title_key(title: str) -> str:
    """SHA-1 of the title after NFKC + casefold, non-alphanumerics dropped and whitespace collapsed; ``""`` if empty."""
    folded = unicodedata.normalize("NFKC", title).casefold()
    kept = "".join(c if c.isalnum() else " " for c in folded)
    text = " ".join(kept.split())
    return hashlib.sha1(text.encode("utf-8")).hexdigest() if text else ""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _newest_first(published: date | None, seq: int) -> tuple[bool, int, int]:
    return (published is None, -(published.toordinal() if published else 0), seq)


def normalise(
    results: Sequence[QueryResult],
    config: NewsConfig,
    today: date,
    adapter_priority: Mapping[str, int] | None = None,
) -> Normalised:
    """Clean, validate, dedupe and cap ``results`` (given in plan order)."""
    priority = adapter_priority or {}
    floor = today - timedelta(days=config.lookback_days)
    ceiling = today + timedelta(days=1)
    rejections: list[Rejection] = []

    # 1-3: clean text, check URL, recency.
    survivors: list[_Entry] = []
    seq = 0
    for result in results:
        for raw in result.items:
            seq += 1
            title = clip(strip_html(raw.title), config.title_max_chars)
            snippet = clip(strip_html(raw.snippet), config.snippet_max_chars)
            if not url_acceptable(raw.url):
                rejections.append(Rejection("normalise", "bad_url", _sha256(raw.url), result.adapter))
                continue
            norm_url = normalise_url(raw.url)
            item_id = _sha256(norm_url)
            if not title:
                rejections.append(Rejection("normalise", "bad_item", item_id, result.adapter))
                continue
            published = raw.published
            if published is not None and published > ceiling:
                rejections.append(Rejection("normalise", "bad_date", item_id, result.adapter))
                continue
            if published is not None and published < floor and result.query.kind != "earnings":
                rejections.append(Rejection("normalise", "stale", item_id, result.adapter))
                continue
            focus = None if result.query.kind in _NO_FOCUS else result.query.ticker
            cleaned = RawItem(title, snippet, raw.url, published, raw.domain)
            prepared = PreparedItem(cleaned, item_id, result.index, focus, result.adapter)
            survivors.append(_Entry(prepared, title_key(title), seq))

    # 4a: dedupe by normalised URL, first seen in plan order wins (so its query supplies the focus ticker).
    by_url: dict[str, _Entry] = {}
    for entry in survivors:
        if entry.prepared.item_id in by_url:
            rejections.append(Rejection("normalise", "dup_url", entry.prepared.item_id, entry.prepared.adapter))
        else:
            by_url[entry.prepared.item_id] = entry

    # 4b: dedupe syndicated titles; the earliest published wins, then adapter priority, then plan order.
    def rank(e: _Entry) -> tuple[date, int, int, int]:
        p = e.prepared
        return (p.item.published or date.max, priority.get(p.adapter, len(priority)), p.query_index, e.seq)

    winners: dict[str, _Entry] = {}
    for entry in by_url.values():
        key = entry.title_key
        if key and key in winners:
            current = winners[key]
            loser = entry if rank(current) <= rank(entry) else current
            if loser is current:
                winners[key] = entry
            rejections.append(Rejection("normalise", "dup_title", loser.prepared.item_id, loser.prepared.adapter))
        elif key:
            winners[key] = entry
    kept = [e for e in by_url.values() if not e.title_key or winners[e.title_key] is e]

    # 5: per-query cap, newest first.
    per_query: dict[int, list[_Entry]] = {}
    for entry in kept:
        per_query.setdefault(entry.prepared.query_index, []).append(entry)
    queues: list[list[_Entry]] = []
    for index in sorted(per_query):
        ordered = sorted(per_query[index], key=lambda e: _newest_first(e.prepared.item.published, e.seq))
        for dropped in ordered[config.max_items_per_query :]:
            rejections.append(Rejection("normalise", "cap_query", dropped.prepared.item_id, dropped.prepared.adapter))
        queues.append(ordered[: config.max_items_per_query])

    # 6: round-robin across queries in plan order up to the cycle budget.
    limit = config.max_items_per_cycle
    out: list[PreparedItem] = []
    depth = 0
    while any(depth < len(q) for q in queues):
        for q in queues:
            if depth < len(q):
                if len(out) < limit:
                    out.append(q[depth].prepared)
                else:
                    rejections.append(
                        Rejection("normalise", "cap_items", q[depth].prepared.item_id, q[depth].prepared.adapter)
                    )
        depth += 1
    return Normalised(out, rejections)
