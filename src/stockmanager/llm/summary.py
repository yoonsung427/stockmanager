"""AI stock summaries: one holding in, a short plain-text note out.

The single generator behind all three triggers (Discord query, daily digest, price-threshold alert). It talks to
whatever ``LLMBackend`` it is given; the composition root wires an ``OpenAILLMBackend`` pointed at the local
llama.cpp server (``summary.base_url``), never the Anthropic backend. Output is free text, not JSON.

News text is untrusted. It is clipped, tag-stripped and placed in a delimited block that the system prompt marks as
data; the model's reply is only ever posted as text, it can never reach the books. The summary prompt is loaded and
sha256-verified like the other prompts (``prompts/summary/v1/meta.yaml``). Prices come from the price providers only,
never from news text (SS §6).
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from importlib.resources import files

import jinja2
import jinja2.sandbox
import yaml

from stockmanager.config.summary import SummaryConfig
from stockmanager.domain.money import q4
from stockmanager.llm.protocol import LLMBackend, LLMError

log = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
MAX_NEWS_ITEMS = 6
TITLE_MAX_CHARS = 200
SNIPPET_MAX_CHARS = 400

_TAG = re.compile(r"<[^>]*>")
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"^.*?</think>", re.DOTALL | re.IGNORECASE)


class SummaryError(Exception):
    """No usable summary came back: the prompt failed its integrity check, the call failed, or the reply was empty."""


@dataclass(frozen=True)
class NewsItem:
    """One recent headline for a ticker, already normalised by the caller."""

    title: str
    snippet: str = ""
    source: str = ""
    published: date | None = None


@dataclass(frozen=True)
class PositionInfo:
    """What we hold and how it is doing. ``price`` is ``None`` when no provider could price it (fail closed)."""

    shares: Decimal
    cost_basis: Decimal  # total dollars paid, as in ``Position.cost_basis``
    price: Decimal | None = None
    price_date: date | None = None
    day_change_pct: Decimal | None = None  # vs the previous close, if known
    thesis: str = ""

    @property
    def market_value(self) -> Decimal | None:
        """``shares * price`` (4 dp), or ``None`` without a price."""
        return None if self.price is None else q4(self.shares * self.price)

    @property
    def pnl(self) -> Decimal | None:
        """Unrealised P&L in dollars, or ``None`` without a price."""
        value = self.market_value
        return None if value is None else q4(value - self.cost_basis)

    @property
    def pnl_pct(self) -> Decimal | None:
        """Unrealised P&L as a percentage of cost basis, or ``None`` without a price or a cost basis."""
        pnl = self.pnl
        if pnl is None or self.cost_basis == 0:
            return None
        return q4(pnl / self.cost_basis * 100)


@dataclass(frozen=True)
class SummaryPrompt:
    """The verified prompt version: the system text and the compiled user template."""

    version: str
    system: str
    user_template: jinja2.Template


def load_summary_prompt(version: str = PROMPT_VERSION) -> SummaryPrompt:
    """Load ``prompts/summary/<version>``, checking each file's sha256 against ``meta.yaml``. Raises ``SummaryError``."""
    directory = files("stockmanager") / "prompts" / "summary" / version
    try:
        meta = yaml.safe_load((directory / "meta.yaml").read_text(encoding="utf-8"))
        texts: dict[str, str] = {}
        for name, expected in meta["files"].items():
            data = (directory / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise SummaryError(f"summary/{version}/{name} does not match meta.yaml")
            texts[name] = data.decode("utf-8")
        env = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined, autoescape=False)
        return SummaryPrompt(version, texts["system.md"].strip(), env.from_string(texts["user.md.j2"]))
    except SummaryError:
        raise
    except (OSError, KeyError, TypeError, ValueError, AttributeError, yaml.YAMLError, jinja2.TemplateError) as e:
        raise SummaryError(f"cannot load summary prompt: {type(e).__name__}") from e


def _clean(text: str, limit: int) -> str:
    """Untrusted text -> one line: tags removed, whitespace collapsed, angle brackets neutralised, clipped."""
    flat = " ".join(_TAG.sub(" ", text).split())
    return flat.replace("<", "&lt;").replace(">", "&gt;")[:limit]


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{q4(value):,.2f}"


def _pct(value: Decimal | None) -> str | None:
    return None if value is None else f"{q4(value):+.2f}"


def render_user_prompt(
    prompt: SummaryPrompt, ticker: str, news_items: Sequence[NewsItem], position_info: PositionInfo, session_date: date
) -> str:
    """Fill the user template. Every number is formatted from ``Decimal``; news text is cleaned and capped."""
    shares = position_info.shares
    position = {
        "shares": f"{shares.normalize():f}",
        "avg_cost": _money(position_info.cost_basis / shares if shares else None),
        "cost_basis": _money(position_info.cost_basis),
        "price": _money(position_info.price),
        "price_date": position_info.price_date.isoformat() if position_info.price_date else "unknown date",
        "market_value": _money(position_info.market_value),
        "pnl": _money(position_info.pnl),
        "pnl_pct": _pct(position_info.pnl_pct),
        "day_change_pct": _pct(position_info.day_change_pct),
        "thesis": _clean(position_info.thesis, SNIPPET_MAX_CHARS),
    }
    news = [
        {
            "title": _clean(n.title, TITLE_MAX_CHARS),
            "snippet": _clean(n.snippet, SNIPPET_MAX_CHARS),
            "source": _clean(n.source, 60) or "unknown source",
            "published": n.published.isoformat() if n.published else None,
        }
        for n in news_items[:MAX_NEWS_ITEMS]
    ]
    return prompt.user_template.render(
        ticker=ticker, session_date=session_date.isoformat(), position=position, news=news
    )


def _strip_reasoning(text: str) -> str:
    """Drop ``<think>...</think>`` blocks that reasoning models (Qwen3 and friends) may put in ``content``."""
    text = _THINK_BLOCK.sub("", text)
    if "</think>" in text.lower():  # an opening tag the server already consumed
        text = _THINK_OPEN.sub("", text)
    return text.strip()


def generate_summary(
    ticker: str,
    news_items: Sequence[NewsItem],
    position_info: PositionInfo,
    session_date: date,
    config: SummaryConfig,
    backend: LLMBackend,
) -> str:
    """Ask the model for a concise summary of ``ticker`` and return its text.

    Raises ``SummaryError`` if the prompt fails verification, the call fails (``LLMError``), the reply was cut off
    or refused, or nothing is left after removing reasoning blocks.
    """
    prompt = load_summary_prompt()
    user = render_user_prompt(prompt, ticker, news_items, position_info, session_date)
    try:
        result = backend.chat_completion(
            model=config.model,
            system=prompt.system,
            user=user,
            max_tokens=config.max_tokens,
            timeout=float(config.timeout_s),
        )
    except LLMError as e:
        raise SummaryError(f"LLM call failed for {ticker}: {type(e).__name__}") from e
    text = _strip_reasoning(result.text)
    if result.stop_reason == "refusal" or not text:
        raise SummaryError(f"LLM returned no summary for {ticker} (stop_reason={result.stop_reason})")
    if result.stop_reason == "max_tokens":
        log.warning("summary for %s hit max_tokens=%d; posting the truncated text", ticker, config.max_tokens)
    return text
