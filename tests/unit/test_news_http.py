"""The news HTTP transport against a local server: http scheme, 2 MB cap, same-host redirects only."""

import socketserver
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from stockmanager.news.fetchers.base import MAX_BODY_BYTES, NewsUnavailable, default_get, domain_of


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/ok":
            self._send(200, b'{"ok": true}')
        elif self.path == "/big":
            self._send(200, b"x" * (MAX_BODY_BYTES + 10))
        elif self.path.startswith("/hop/"):
            n = int(self.path.split("/")[2])
            self._send(302, b"", {"Location": f"/hop/{n + 1}" if n < 9 else "/ok"})
        elif self.path == "/to-ok":
            self._send(301, b"", {"Location": "/ok"})
        elif self.path == "/offsite":
            self._send(302, b"", {"Location": "http://example.invalid/x"})
        else:
            self._send(404, b"")

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


class QuickServer(HTTPServer):
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)  # HTTPServer.server_bind calls getfqdn(), which can take 30 s offline
        self.server_name, self.server_port = "127.0.0.1", self.socket.getsockname()[1]


@pytest.fixture
def base() -> Iterator[str]:
    server = QuickServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_plain_http_get(base: str) -> None:
    resp = default_get(f"{base}/ok", {"Accept": "application/json"})
    assert (resp.status, resp.body) == (200, b'{"ok": true}')


def test_body_over_two_megabytes_is_refused(base: str) -> None:
    with pytest.raises(NewsUnavailable, match="too large"):
        default_get(f"{base}/big", {})


def test_same_host_redirect_is_followed(base: str) -> None:
    assert default_get(f"{base}/to-ok", {}).body == b'{"ok": true}'


def test_off_host_redirect_is_not_followed(base: str) -> None:
    assert default_get(f"{base}/offsite", {}).status == 302


def test_redirect_chain_is_limited_to_three_hops(base: str) -> None:
    assert default_get(f"{base}/hop/7", {}).status == 200  # exactly three redirects
    assert default_get(f"{base}/hop/6", {}).status == 310  # a fourth is refused


def test_non_http_scheme_refused() -> None:
    with pytest.raises(NewsUnavailable):
        default_get("file:///etc/passwd", {})


def test_domain_of() -> None:
    assert domain_of("HTTPS://WWW.Example.COM:8443/a") == "example.com"
    assert domain_of("not a url") == ""
    assert domain_of("https://[::1/") == ""
