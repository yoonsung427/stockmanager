"""Minimal synchronous HTTP GET with split timeouts and bounded retries, injectable for tests."""

from __future__ import annotations

import http.client
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from stockmanager.pricing.errors import PriceError

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0
MAX_RETRIES = 2
MAX_RETRY_AFTER = 30.0
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass(frozen=True)
class HttpResponse:
    """Status, body and lower-cased headers of one response."""

    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class HttpGet(Protocol):
    """One GET attempt. Raises ``TimeoutError`` or ``OSError`` on transport failure."""

    def __call__(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        """Fetch ``url``."""
        ...


def urllib_get(url: str, headers: Mapping[str, str]) -> HttpResponse:
    """Default transport: connect timeout 5 s, then read timeout 15 s. Redirects are not followed."""
    parts = urlsplit(url)
    conn = http.client.HTTPSConnection(parts.netloc, timeout=CONNECT_TIMEOUT)
    try:
        conn.connect()
        if conn.sock is not None:
            conn.sock.settimeout(READ_TIMEOUT)
        conn.request("GET", f"{parts.path}?{parts.query}" if parts.query else parts.path, headers=dict(headers))
        resp = conn.getresponse()
        return HttpResponse(resp.status, resp.read(), {k.lower(): v for k, v in resp.getheaders()})
    finally:
        conn.close()


def get_with_retries(
    get: HttpGet,
    url: str,
    headers: Mapping[str, str],
    ticker: str,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """GET ``url`` and return the body of a 200 response.

    Retries up to ``MAX_RETRIES`` times on 429/5xx (honouring a capped ``Retry-After``). Raises
    ``PriceError("timeout")`` on a transport timeout or error and ``PriceError("http_error")`` otherwise.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = get(url, headers)
        except TimeoutError as e:
            raise PriceError("timeout", ticker, str(e) or "timed out") from e
        except OSError as e:
            raise PriceError("timeout", ticker, f"transport error: {type(e).__name__}") from e
        if resp.status == 200:
            return resp.body
        retryable = resp.status == 429 or resp.status >= 500
        if not retryable or attempt == MAX_RETRIES:
            raise PriceError("http_error", ticker, f"HTTP {resp.status}")
        sleep(_backoff(resp, attempt))
    raise AssertionError("unreachable")  # pragma: no cover


def _backoff(resp: HttpResponse, attempt: int) -> float:
    try:
        wait = float(resp.headers.get("retry-after", ""))
    except ValueError:
        wait = 0.5 * 2**attempt
    return min(max(wait, 0.0), MAX_RETRY_AFTER)
