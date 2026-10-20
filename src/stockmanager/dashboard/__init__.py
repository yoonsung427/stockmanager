"""Dashboard generation: state files -> a single static HTML page (AS §9.3).

Pure pipeline: ``build_dashboard`` reads the workspace and computes a ``DashboardModel``; ``render_dashboard``
turns it into HTML with Jinja2 (sandboxed, strict) and inline SVG; ``write_dashboard`` writes the result to
``workspace/dashboard/`` atomically. There is no JavaScript anywhere in the output.
"""

from __future__ import annotations

from stockmanager.dashboard.build import build_dashboard
from stockmanager.dashboard.model import DashboardModel
from stockmanager.dashboard.render import render_dashboard, render_evaluation
from stockmanager.dashboard.write import write_dashboard

__all__ = [
    "DashboardModel",
    "build_dashboard",
    "render_dashboard",
    "render_evaluation",
    "write_dashboard",
]
