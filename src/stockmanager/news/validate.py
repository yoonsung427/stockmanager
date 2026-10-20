"""Validation and sanitisation of extracted candidates (AS §7.5, news-sources §4.4). Deterministic.

A candidate becomes a ``Fact`` only if every check passes; otherwise it is dropped with a reason code and never trimmed
or repaired. The one sanctioned cut is the 200-character description limit, applied before the non-empty check.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any, Literal, cast, get_args

from stockmanager.domain.docs import Fact, NewsEvent
from stockmanager.news.config import TICKER_RE
from stockmanager.news.extract import Candidate
from stockmanager.news.fetchers.base import valid_domain
from stockmanager.news.normalise import PreparedItem
from stockmanager.news.raw import url_acceptable

RejectReason = Literal[
    "val_shape",
    "val_ticker_syntax",
    "val_ticker_relevance",
    "val_date_future",
    "val_date_old",
    "val_description_empty",
    "val_command_lookalike",
    "val_url",
]

DESCRIPTION_MAX_CHARS = 200
DISPLAY_TITLE_MAX_CHARS = 300
MAX_DAYS_AHEAD = 90
MAX_DAYS_BACK = 60
_FIELDS = frozenset({"ticker", "event", "description", "date"})
_EVENTS = frozenset(get_args(NewsEvent))
_LINE_BREAKS = str.maketrans({c: " " for c in "\n\r\t\v\f\x85  "})
_COMMAND_START = re.compile(r"[\W_]*(?:VETO|APPROVE|PAUSE|RESUME|SET|CLOSE|RECONCILE|RECONFIRM)\b", re.IGNORECASE)
_COMMAND_ANYWHERE = re.compile(r"ignore\s+previous|system\s*:", re.IGNORECASE)


def sanitise(text: str, limit: int) -> str:
    """NFC, drop Cc/Cf (control and zero-width), collapse whitespace to one line, cut to ``limit``.

    Line breaks and tabs become spaces first so "a\\nb" does not fuse into "ab"; every other Cc/Cf character is
    removed outright so zero-width splitting of a word cannot hide it from the lookalike check.
    """
    nfc = unicodedata.normalize("NFC", text).translate(_LINE_BREAKS)
    kept = "".join(c for c in nfc if unicodedata.category(c) not in ("Cc", "Cf"))
    return " ".join(kept.split())[:limit].rstrip()


def fact_id(ticker: str, event: str, when: date, description: str, source_url: str) -> str:
    """Deterministic id so replays and caches reproduce it: ``"f_" + sha256(ticker|event|date|description|url)[:12]``."""
    digest = hashlib.sha256(f"{ticker}|{event}|{when.isoformat()}|{description}|{source_url}".encode()).hexdigest()
    return "f_" + digest[:12]


def _as_candidate(candidate: Candidate | Mapping[str, Any]) -> Candidate | RejectReason:
    if isinstance(candidate, Candidate):
        return candidate
    if set(candidate) != _FIELDS:
        return "val_shape"
    ticker, event, description, raw_date = (candidate[k] for k in ("ticker", "event", "description", "date"))
    if not (isinstance(ticker, str) and isinstance(description, str) and isinstance(event, str)):
        return "val_shape"
    if event not in _EVENTS:
        return "val_shape"
    try:
        when = date.fromisoformat(raw_date) if isinstance(raw_date, str) else None
    except ValueError:
        when = None
    if when is None:
        return "val_date_future"  # AS §7.5: an invalid date is reported with the future-date code
    return Candidate(ticker=ticker, event=cast(NewsEvent, event), description=description, date=when)


def validate_candidate(
    candidate: Candidate | Mapping[str, Any],
    item: PreparedItem,
    relevance: frozenset[str],
    today: date,
) -> Fact | RejectReason:
    """Turn one candidate into a ``Fact`` or return the reason it was dropped.

    Provenance (``source_url``, ``source_domain``, ``display_title``) comes from the fetcher's record. ``Fact.date`` is
    the fetcher's ``published`` when it supplied one, else the validated model date (news-sources §4.4, literal AS §7.3
    reading; §9 item 3 records that this loses the event date of a forward-dated headline and needs a decision).
    """
    parsed = _as_candidate(candidate)
    if isinstance(parsed, str):
        return parsed
    if not TICKER_RE.fullmatch(parsed.ticker):
        return "val_ticker_syntax"
    if parsed.ticker not in relevance:
        return "val_ticker_relevance"
    if parsed.date > today + timedelta(days=MAX_DAYS_AHEAD):
        return "val_date_future"
    if parsed.date < today - timedelta(days=MAX_DAYS_BACK):
        return "val_date_old"
    description = sanitise(parsed.description, DESCRIPTION_MAX_CHARS)
    if not description:
        return "val_description_empty"
    if _COMMAND_START.match(description) or _COMMAND_ANYWHERE.search(description):
        return "val_command_lookalike"
    raw = item.item
    if not url_acceptable(raw.url) or not valid_domain(raw.domain):
        return "val_url"
    when = raw.published or parsed.date
    return Fact(
        fact_id=fact_id(parsed.ticker, parsed.event, when, description, raw.url),
        ticker=parsed.ticker,
        event=parsed.event,
        description=description,
        date=when,
        source_url=raw.url,
        source_domain=raw.domain,
        display_title=sanitise(raw.title, DISPLAY_TITLE_MAX_CHARS) or raw.domain,
    )
