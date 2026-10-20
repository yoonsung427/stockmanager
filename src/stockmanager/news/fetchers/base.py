"""Shared pieces for the news fetchers: the failure type, the HTTP transport and the retry policy.

Transport differs from the price one: it speaks ``http`` as well as ``https`` (SearXNG is usually plain http on the
LAN), caps the body at 2 MB and follows redirects only to the same host, at most three hops (news-sources §1).
"""

from __future__ import annotations

import http.client
import re
import time
from collections.abc import Callable, Mapping
from typing import Literal
from urllib.parse import urljoin, urlsplit

from stockmanager.adapters.http import CONNECT_TIMEOUT, MAX_RETRY_AFTER, READ_TIMEOUT, USER_AGENT, HttpGet, HttpResponse
from stockmanager.domain.ports import NewsFetcher

__all__ = [
    "MAX_BODY_BYTES",
    "USER_AGENT",
    "NewsFetcher",
    "NewsUnavailable",
    "default_get",
    "domain_of",
    "fetch_body",
]

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_DOMAIN_RE = re.compile(r"[a-z0-9.\-]{1,253}")


class NewsUnavailable(Exception):
    """Raised when a fetch call fails for adapter-level reasons.

    ``kind`` is ``"error"`` for an ordinary failure, ``"auth"`` for 401/403 or a missing key (the adapter is disabled for
    the rest of the cycle) and ``"rate"`` when the rate limiter refused to wait (not the adapter's fault).
    """

    def __init__(self, detail: str = "", *, kind: Literal["error", "auth", "rate"] = "error") -> None:
        super().__init__(detail)
        self.kind = kind


def domain_of(url: str) -> str:
    """Lower-cased host of ``url`` without a leading ``www.``; ``""`` when there is none."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    return host.removeprefix("www.")


def valid_domain(domain: str) -> bool:
    """True iff ``domain`` matches ``^[a-z0-9.\\-]{1,253}$`` (news-sources §7.2)."""
    return _DOMAIN_RE.fullmatch(domain) is not None


def default_get(url: str, headers: Mapping[str, str]) -> HttpResponse:
    """One GET: connect 5 s, read 15 s, body capped at ``MAX_BODY_BYTES``, same-host redirects up to 3 hops."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise NewsUnavailable("unsupported URL scheme")
        conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parts.netloc, timeout=CONNECT_TIMEOUT)
        try:
            conn.connect()
            if conn.sock is not None:
                conn.sock.settimeout(READ_TIMEOUT)
            target = f"{parts.path or '/'}?{parts.query}" if parts.query else (parts.path or "/")
            conn.request("GET", target, headers=dict(headers))
            resp = conn.getresponse()
            location = resp.getheader("Location")
            if resp.status in _REDIRECTS and location:
                nxt = urlsplit(urljoin(current, location))
                if (nxt.scheme, nxt.netloc.lower()) != (parts.scheme, parts.netloc.lower()):
                    return HttpResponse(resp.status, b"", {})  # off-host redirect: surfaces as a failure
                current = nxt.geturl()
                continue
            body = resp.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                raise NewsUnavailable("response body too large")
            return HttpResponse(resp.status, body, {k.lower(): v for k, v in resp.getheaders()})
        finally:
            conn.close()
    return HttpResponse(310, b"", {})  # too many redirects


def fetch_body(
    get: HttpGet,
    url: str,
    headers: Mapping[str, str],
    *,
    before_attempt: Callable[[], object] = lambda: None,
    sleep: Callable[[float], None] = time.sleep,
    retry_429_wait: float = 1.0,
    retry_wait: float = 1.0,
) -> bytes:
    """GET ``url`` and return the body of a 200 response (AS §10.3: one retry).

    Retries once on timeout, connection error, 5xx and 429 (honouring a capped ``Retry-After``, else ``retry_429_wait``);
    never on other 4xx. 401/403 raise ``NewsUnavailable(kind="auth")`` at once. ``before_attempt`` runs before every
    request so a rate limiter sees each real call.
    """
    for attempt in range(2):
        wait = retry_wait
        try:
            before_attempt()
            resp = get(url, headers)
        except NewsUnavailable:
            raise
        except TimeoutError:
            failure = "timeout"
        except OSError as e:
            failure = f"connection error: {type(e).__name__}"
        else:
            if resp.status == 200:
                return resp.body
            if resp.status in (401, 403):
                raise NewsUnavailable(f"HTTP {resp.status}", kind="auth")
            if resp.status != 429 and resp.status < 500:
                raise NewsUnavailable(f"HTTP {resp.status}")
            failure = f"HTTP {resp.status}"
            if resp.status == 429:
                try:
                    wait = min(max(float(resp.headers.get("retry-after", "")), 0.0), MAX_RETRY_AFTER)
                except ValueError:
                    wait = retry_429_wait
        if attempt == 1:
            raise NewsUnavailable(failure)
        sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover
