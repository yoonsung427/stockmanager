"""Yahoo RSS adapter: defusedxml parsing, HTML stripping, hostile XML rejected."""

from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from stockmanager.adapters.http import HttpResponse
from stockmanager.news.fetchers.base import MAX_BODY_BYTES, NewsUnavailable
from stockmanager.news.fetchers.yahoo_rss import YahooRssFetcher
from tests.support.news import ScriptedGet, query

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Yahoo! Finance: AAPL</title>
<item>
  <title>Apple &amp; Co. beat <b>estimates</b></title>
  <description>&lt;p&gt;Shares rose &lt;a href="https://x"&gt;today&lt;/a&gt; &amp;amp; more&lt;/p&gt;</description>
  <link>https://finance.yahoo.com/news/apple-beats.html?utm_source=rss</link>
  <pubDate>Sat, 17 Oct 2026 13:45:00 +0000</pubDate>
</item>
<item><title>No date</title><link>https://www.example.com/x</link></item>
<item><description>no title or link</description><pubDate>garbage</pubDate></item>
</channel></rss>"""


def fetch(body: bytes, status: int = 200, ticker: str = "AAPL") -> list:  # type: ignore[type-arg]
    return YahooRssFetcher(http_get=ScriptedGet(HttpResponse(status, body)), sleep=lambda _: None).fetch(
        query("holding", ticker)
    )


def test_request_url() -> None:
    get = ScriptedGet(HttpResponse(200, FEED))
    YahooRssFetcher(http_get=get).fetch(query("holding", "BRK.B"))
    parts = urlsplit(get.seen[0][0])
    assert (parts.netloc, parts.path) == ("feeds.finance.yahoo.com", "/rss/2.0/headline")
    assert parse_qs(parts.query) == {"s": ["BRK.B"], "region": ["US"], "lang": ["en-US"]}


def test_parses_items_and_strips_html() -> None:
    first, second, third = fetch(FEED)
    assert first.title == "Apple & Co. beat estimates"
    assert first.snippet == "Shares rose today & more"
    assert first.url == "https://finance.yahoo.com/news/apple-beats.html?utm_source=rss"
    assert first.domain == "finance.yahoo.com" and first.published == date(2026, 10, 17)
    assert (second.published, second.domain, second.snippet) == (None, "example.com", "")
    assert (third.title, third.url, third.published) == ("", "", None)  # left for normalisation to reject


def test_empty_channel_is_success() -> None:
    assert fetch(b'<rss version="2.0"><channel></channel></rss>') == []


def test_external_entity_is_rejected() -> None:
    xxe = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><item><title>&x;</title></item></channel></rss>'
    with pytest.raises(NewsUnavailable):
        fetch(xxe)


def test_billion_laughs_is_rejected() -> None:
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
        b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]><rss><channel><item><title>&lol3;</title></item></channel></rss>'
    )
    with pytest.raises(NewsUnavailable):
        fetch(bomb)


def test_any_doctype_is_rejected() -> None:
    with pytest.raises(NewsUnavailable):
        fetch(b'<?xml version="1.0"?><!DOCTYPE rss><rss><channel/></rss>')


@pytest.mark.parametrize("body", [b"not xml", b"<rss><channel>", b""])
def test_malformed_xml(body: bytes) -> None:
    with pytest.raises(NewsUnavailable):
        fetch(body)


def test_http_failure_and_bad_ticker() -> None:
    with pytest.raises(NewsUnavailable):
        fetch(b"", status=500)
    with pytest.raises(NewsUnavailable):
        fetch(FEED, ticker="AAPL&evil=1")


def test_oversize_limit_is_two_megabytes() -> None:
    assert MAX_BODY_BYTES == 2 * 1024 * 1024
