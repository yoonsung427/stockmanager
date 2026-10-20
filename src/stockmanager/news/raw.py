"""``RawItem``: the one typed home of untrusted news text (AS §7.3), plus pure text helpers.

Only ``news.extract`` and the dashboard's display path may read ``title`` or ``snippet`` after normalisation.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

TITLE_MAX_CHARS = 300
SNIPPET_MAX_CHARS = 2000
URL_MAX_CHARS = 2048

_TAG = re.compile(r"<[^>]*>")


@dataclass(frozen=True)
class RawItem:
    """A headline and snippet as the fetcher recorded them."""

    title: str
    snippet: str
    url: str
    published: date | None
    domain: str

    def __repr__(self) -> str:
        """Redacted: no raw text (AS §7.3)."""
        return f"RawItem(url={self.url!r}, domain={self.domain!r})"


def strip_html(text: str) -> str:
    """Remove tags, decode entities once and collapse whitespace."""
    return " ".join(html.unescape(_TAG.sub(" ", text)).split())


def clip(text: str, limit: int) -> str:
    """Truncate to ``limit`` characters (AS §7.2: truncated, not rejected)."""
    return text[:limit]


def url_acceptable(url: str) -> bool:
    """True iff ``url`` is http(s), at most 2048 chars, has a host, no userinfo and a valid port (AS §7.5)."""
    if not url or len(url) > URL_MAX_CHARS:
        return False
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for a malformed port
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname) and "@" not in parts.netloc
