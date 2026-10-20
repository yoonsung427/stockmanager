"""Jinja2 (Sandboxed, StrictUndefined) -> HTML, plus inline SVG chart helpers (AS §9.3, ⚑AD18).

No JavaScript anywhere: tables are server-rendered and charts are plain SVG built in Python. Every untrusted
string (thesis, news titles/descriptions, Reasoner prose, veto reasons) flows through Jinja2 autoescape and is
never marked safe (TS DASH-11).
"""

from __future__ import annotations

import html
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from jinja2 import PackageLoader, StrictUndefined, select_autoescape
from jinja2.sandbox import SandboxedEnvironment

from stockmanager.dashboard.model import DashboardModel, NAVPoint, SectorWeight

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"
)
_SAFE_SCHEMES = {"http", "https"}


def _is_safe_url(url: str) -> bool:
    """True iff ``url`` parses and uses ``http``/``https`` only (AS §9.3: other schemes render as plain text)."""
    try:
        return urlparse(url).scheme in _SAFE_SCHEMES
    except ValueError:
        return False


def _fmt_money(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.2f}"


def _fmt_pct(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}%"


def _fmt_decimal(value: Decimal | None, places: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{places}f}"


def _fmt_shares(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}"


def _build_env() -> SandboxedEnvironment:
    env = SandboxedEnvironment(
        loader=PackageLoader("stockmanager.dashboard", "templates"),
        autoescape=select_autoescape(default=True, default_for_string=True),
        undefined=StrictUndefined,
    )
    env.filters["money"] = _fmt_money
    env.filters["pct"] = _fmt_pct
    env.filters["decimal"] = _fmt_decimal
    env.filters["shares"] = _fmt_shares
    env.filters["safe_url"] = _is_safe_url
    env.globals["csp"] = CSP
    return env


_ENV = _build_env()


def render_dashboard(model: DashboardModel) -> str:
    """Render the full dashboard HTML page from ``model``. Pure: no I/O."""
    template = _ENV.get_template("portfolio.html.j2")
    return template.render(m=model, svg_line_chart=svg_line_chart, svg_donut_chart=svg_donut_chart)


def render_evaluation(eval_json: dict[str, Any]) -> str:
    """Render a single evaluation page from its parsed JSON document."""
    template = _ENV.get_template("eval.html.j2")
    return template.render(e=eval_json)


# -- SVG chart helpers ---------------------------------------------------------------------------------------------


def svg_line_chart(
    series: list[tuple[str, list[NAVPoint]]], title: str, width: int = 640, height: int = 280
) -> str:
    """An inline SVG line chart for one or more series, each rebased for comparability on the same axes.

    ``series`` is ``[(label, points), ...]``. Every rendered label is HTML-escaped; nothing here is marked safe
    for Jinja2 — the caller still receives plain text that autoescape will re-escape harmlessly if re-inserted.
    """
    pad = 32
    colors = ["#2563eb", "#dc2626", "#16a34a", "#9333ea"]
    all_points = [p for _, pts in series for p in pts]
    if not all_points:
        return f'<svg role="img" aria-label="{html.escape(title)}" width="{width}" height="{height}"></svg>'
    values = [p.value for p in all_points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or Decimal(1)
    plot_w = width - 2 * pad
    plot_h = height - 2 * pad

    def xy(index: int, n: int, value: Decimal) -> tuple[float, float]:
        x = pad + (plot_w * index / (n - 1) if n > 1 else plot_w / 2)
        y = pad + plot_h * (1 - float((value - lo) / span))
        return x, y

    parts = [
        (
            f'<svg role="img" aria-label="{html.escape(title)}" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        ),
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="none" stroke="#d1d5db"/>',
    ]
    for s_index, (label, points) in enumerate(series):
        if not points:
            continue
        color = colors[s_index % len(colors)]
        n = len(points)
        coords = [xy(i, n, p.value) for i, p in enumerate(points)]
        path = " ".join(f"{'M' if i == 0 else 'L'}{x:.2f},{y:.2f}" for i, (x, y) in enumerate(coords))
        parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2"/>')
        for (x, y), p in zip(coords, points):
            safe_label = html.escape(f"{label} {p.date.isoformat()}: {p.value}")
            parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="{color}"><title>{safe_label}</title></circle>')
    parts.append("</svg>")
    return "".join(parts)


def svg_donut_chart(sectors: list[SectorWeight], title: str, size: int = 240) -> str:
    """An inline SVG donut chart of sector (and cash) weights, with a text legend below."""
    cx = cy = size / 2
    radius = size / 2 - 10
    inner = radius * 0.6
    palette = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#4b5563", "#ca8a04"]
    total = sum((s.weight_pct for s in sectors), Decimal(0))
    parts = [
        (
            f'<svg role="img" aria-label="{html.escape(title)}" viewBox="0 0 {size} {size}" '
            f'width="{size}" height="{size}" xmlns="http://www.w3.org/2000/svg">'
        )
    ]
    if total <= 0:
        parts.append("</svg>")
        return "".join(parts)
    angle = -90.0
    for index, s in enumerate(sectors):
        fraction = float(s.weight_pct / total)
        sweep = fraction * 360.0
        color = palette[index % len(palette)]
        parts.append(_donut_slice(cx, cy, radius, inner, angle, angle + sweep, color, f"{s.sector}: {s.weight_pct}%"))
        angle += sweep
    parts.append("</svg>")
    legend = "".join(
        f'<li><span style="background:{palette[i % len(palette)]}" class="swatch"></span>'
        f"{html.escape(s.sector)}: {s.weight_pct}%</li>"
        for i, s in enumerate(sectors)
    )
    return "".join(parts) + f'<ul class="legend">{legend}</ul>'


def _donut_slice(cx: float, cy: float, r: float, inner: float, a0: float, a1: float, color: str, label: str) -> str:
    import math

    def point(radius: float, angle_deg: float) -> tuple[float, float]:
        rad = math.radians(angle_deg)
        return cx + radius * math.cos(rad), cy + radius * math.sin(rad)

    large_arc = 1 if (a1 - a0) % 360 > 180 else 0
    x0, y0 = point(r, a0)
    x1, y1 = point(r, a1)
    xi1, yi1 = point(inner, a1)
    xi0, yi0 = point(inner, a0)
    d = (
        f"M{x0:.2f},{y0:.2f} A{r:.2f},{r:.2f} 0 {large_arc} 1 {x1:.2f},{y1:.2f} "
        f"L{xi1:.2f},{yi1:.2f} A{inner:.2f},{inner:.2f} 0 {large_arc} 0 {xi0:.2f},{yi0:.2f} Z"
    )
    safe_label = html.escape(label)
    return f'<path d="{d}" fill="{color}"><title>{safe_label}</title></path>'
