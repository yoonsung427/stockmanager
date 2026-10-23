"""Dashboard: model, build, render, write, templates (AS §9.3, TS DASH-01..26)."""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stockmanager.dashboard.build import build_dashboard
from stockmanager.dashboard.model import DashboardModel, NAVPoint, SectorWeight
from stockmanager.dashboard.render import render_dashboard, render_evaluation, svg_donut_chart, svg_line_chart
from stockmanager.dashboard.write import model_as_dict, write_dashboard
from stockmanager.domain.docs import PortfolioDoc, Position, ProposalDoc, Trade
from stockmanager.store import FileStore

PT = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 12, 6, 30, tzinfo=PT)
TODAY = datetime(2026, 10, 20, 9, 0, tzinfo=PT)

ALL_SECTIONS = [
    "summary",
    "holdings",
    "performance",
    "sectors",
    "risk",
    "activity",
    "earnings",
    "news",
    "evaluations",
    "status",
]


def clock() -> datetime:
    return TODAY


def make_store(tmp_path: Path) -> FileStore:
    return FileStore(tmp_path / "ws")


def make_trade(n: int = 1, action: str = "BUY", ticker: str = "AAPL", shares: str = "10", price: str = "150") -> Trade:
    return Trade(
        trade_id=f"P-OPEN-2026-10-12-{action.lower()}{n}",
        timestamp=T0,
        action=action,  # type: ignore[arg-type]
        ticker=ticker,
        shares=Decimal(shares),
        fill_price=Decimal(price),
        cost=Decimal(shares) * Decimal(price),
        price_source="yahoo",
        thesis="growth thesis",
    )


def make_position(ticker: str = "AAPL", shares: str = "10", price: str = "150", thesis: str = "growth thesis") -> Position:
    return Position(
        ticker=ticker,
        shares=Decimal(shares),
        fill_price=Decimal(price),
        fill_time=T0,
        cost_basis=Decimal(shares) * Decimal(price),
        thesis=thesis,
        price_source="yahoo",
    )


def make_portfolio(
    positions: list[Position] | None = None,
    trades: list[Trade] | None = None,
    cash: str = "8500",
    start_cash: str = "10000",
    status: str = "LIVE",
    auto_approve: bool = True,
) -> PortfolioDoc:
    return PortfolioDoc(
        start_cash=Decimal(start_cash),
        cash=Decimal(cash),
        status=status,  # type: ignore[arg-type]
        positions=positions or [],
        trades=trades or [],
        config={"benchmark": "SPY", "auto_approve": auto_approve},
    )


def write_nav_row(store: FileStore, date: str, nav: str, benchmark_close: str, prices: dict[str, str]) -> None:
    store.save_nav_history(
        [
            {
                "schema_version": 1,
                "date": date,
                "kind": "valuation",
                "nav": nav,
                "cash": "0",
                "positions_value": "0",
                "benchmark": "SPY",
                "benchmark_close": benchmark_close,
                "source": "yahoo",
                "prices": prices,
                "cycle_id": date,
                "recorded_at": f"{date}T09:00:00-07:00",
            }
        ]
    )


# -- build_dashboard --------------------------------------------------------------------------------------------


def test_build_happy_path_loads_positions_and_nav(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save_portfolio(make_portfolio(positions=[make_position()], trades=[make_trade()]))
    write_nav_row(store, "2026-10-12", "10000.0000", "500.0000", {"AAPL": "150.0000", "SPY": "500.0000"})
    write_nav_row(store, "2026-10-19", "10200.0000", "510.0000", {"AAPL": "170.0000", "SPY": "510.0000"})

    model = build_dashboard(store, clock)

    assert model.positions[0].ticker == "AAPL"
    assert model.positions[0].current_price == Decimal("170.0000")
    assert model.nav > 0
    assert model.status == "LIVE"


def test_build_empty_portfolio_has_cash_only(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save_portfolio(make_portfolio(positions=[], trades=[], cash="10000", start_cash="10000"))

    model = build_dashboard(store, clock)

    assert model.positions == []
    assert model.cash == Decimal(10000)
    assert model.nav == Decimal("10000.0000")
    assert model.cash_pct == Decimal("100.0000")


def test_build_no_nav_history_uses_position_prices_and_empty_history(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save_portfolio(make_portfolio(positions=[make_position()], trades=[make_trade()]))

    model = build_dashboard(store, clock)

    assert model.nav_history == [NAVPoint(date=TODAY.date(), value=model.nav)]
    assert model.benchmark_history == []
    # fallback to cost basis: AAPL avg_cost = 150, no nav_history price available
    assert model.positions[0].current_price == Decimal(150)


def test_build_missing_price_falls_back_to_cost_basis(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save_portfolio(
        make_portfolio(positions=[make_position(ticker="MSFT", price="300"), make_position(ticker="AAPL", price="150")])
    )
    write_nav_row(store, "2026-10-12", "10000.0000", "500.0000", {"AAPL": "170.0000"})  # MSFT not priced

    model = build_dashboard(store, clock)

    by_ticker = {p.ticker: p for p in model.positions}
    assert by_ticker["AAPL"].current_price == Decimal("170.0000")
    assert by_ticker["MSFT"].current_price == Decimal(300)  # fallback


@pytest.mark.parametrize("status", ["LIVE", "PAUSED", "CLOSED"])
def test_build_status_states(tmp_path: Path, status: str) -> None:
    store = make_store(tmp_path)
    store.save_portfolio(make_portfolio(status=status))

    model = build_dashboard(store, clock)

    assert model.status == status


def test_build_pending_proposals_from_disk(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save_portfolio(make_portfolio(positions=[make_position()]))
    proposal = _make_proposal()
    store.save_proposal(proposal.proposal_id, proposal)

    model = build_dashboard(store, clock)

    assert len(model.pending_proposals) == 1
    pending = model.pending_proposals[0]
    assert pending.proposal_id == proposal.proposal_id
    assert pending.command_literal == f"VETO {proposal.proposal_id}"
    assert "SELL" not in pending.legs_summary or "BUY" in pending.legs_summary


def _make_proposal() -> ProposalDoc:
    from stockmanager.domain.docs import CapsSnapshot, ProposalLeg, ProposalSize, TransitionRecord
    from stockmanager.domain.proposal_state import GENESIS_HASH

    leg = ProposalLeg(
        leg="buy1",
        trade_id="P-2026-10-19-01-buy1",
        action="BUY",
        ticker="MSFT",
        size=ProposalSize(kind="target_value", target_value=Decimal(1000)),
        est_shares=Decimal(3),
        est_price=Decimal(300),
        est_value=Decimal(900),
        thesis="diversify",
    )
    transition = TransitionRecord(
        seq=1, from_state=None, to="PROPOSED", at=T0, actor="system:evaluation", cause="created",
        prev_hash=GENESIS_HASH, hash="deadbeef",
    )
    return ProposalDoc(
        proposal_id="P-2026-10-19-01",
        created_at=T0,
        state="PROPOSED",
        approval_mode="auto",
        veto_window_hours=4,
        fill_session_date=T0.date(),
        execute_at=T0,
        expires_at=T0,
        caps=CapsSnapshot(
            max_position_pct=Decimal(30), new_position_max_pct=10, proposal_sector_max_pct=80, max_trades_per_cycle=3
        ),
        legs=[leg],
        rationale="rationale",
        hold_alternative="hold",
        transitions=[transition],
    )


# -- render_dashboard ---------------------------------------------------------------------------------------------


def _model(tmp_path: Path, **portfolio_kwargs: object) -> DashboardModel:
    store = make_store(tmp_path)
    portfolio_kwargs.setdefault("positions", [make_position()])
    portfolio_kwargs.setdefault("trades", [make_trade()])
    store.save_portfolio(make_portfolio(**portfolio_kwargs))  # type: ignore[arg-type]
    write_nav_row(store, "2026-10-12", "10000.0000", "500.0000", {"AAPL": "150.0000", "SPY": "500.0000"})
    return build_dashboard(store, clock)


def test_render_has_all_ten_sections(tmp_path: Path) -> None:
    html = render_dashboard(_model(tmp_path))
    for section in ALL_SECTIONS:
        assert f'data-section="{section}"' in html


def test_render_has_csp_meta_tag(tmp_path: Path) -> None:
    html = render_dashboard(_model(tmp_path))
    assert 'http-equiv="Content-Security-Policy"' in html
    assert "default-src 'none'" in html
    assert "script-src" not in html or "'none'" in html


def test_render_has_no_javascript(tmp_path: Path) -> None:
    html = render_dashboard(_model(tmp_path))
    assert "<script" not in html.lower()
    assert "javascript:" not in html.lower()


def test_render_escapes_untrusted_thesis(tmp_path: Path) -> None:
    html = render_dashboard(_model(tmp_path, positions=[make_position(thesis="<img src=x onerror=alert(1)>")]))
    assert "<img src=x" not in html
    assert "&lt;img" in html


def test_render_escapes_news_description_and_title(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save_portfolio(make_portfolio(positions=[make_position()]))
    _write_fact(
        store,
        ticker="AAPL",
        description="<script>steal()</script>",
        display_title="<b>Breaking</b>",
        source_url="https://example.com/a",
    )
    model = build_dashboard(store, clock)
    html = render_dashboard(model)
    assert "<script>steal" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;" in html


def _write_fact(
    store: FileStore,
    ticker: str,
    description: str,
    display_title: str,
    source_url: str,
    event: str = "other",
    date: str = "2026-10-19",
    fact_id: str = "f1",
) -> None:
    doc = {
        "facts": [
            {
                "fact_id": fact_id,
                "ticker": ticker,
                "event": event,
                "description": description,
                "date": date,
                "source_url": source_url,
                "source_domain": "example.com",
                "display_title": display_title,
            }
        ]
    }
    (store.root / "extracted-facts").mkdir(parents=True, exist_ok=True)
    (store.root / "extracted-facts" / "2026-10-19.json").write_text(json.dumps(doc), encoding="utf-8")


def test_render_news_links_http_only(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save_portfolio(make_portfolio(positions=[make_position()]))
    _write_fact(store, ticker="AAPL", description="d", display_title="t", source_url="javascript:alert(1)")
    model = build_dashboard(store, clock)
    html = render_dashboard(model)
    # non-http(s) schemes render as plain text, never as a clickable href (AS §9.3)
    assert 'href="javascript' not in html
    assert re.search(r'<a[^>]*href="[^"]*javascript', html) is None


def test_render_safe_links_have_noopener_and_target_blank(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save_portfolio(make_portfolio(positions=[make_position()]))
    _write_fact(store, ticker="AAPL", description="d", display_title="t", source_url="https://example.com/x")
    model = build_dashboard(store, clock)
    html = render_dashboard(model)
    assert 'rel="noopener noreferrer"' in html
    assert 'target="_blank"' in html


def test_render_performance_table_in_details(tmp_path: Path) -> None:
    html = render_dashboard(_model(tmp_path))
    assert "<details>" in html
    assert "<summary>" in html


def test_render_status_command_is_code_not_link(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save_portfolio(make_portfolio(positions=[make_position()]))
    proposal = _make_proposal()
    store.save_proposal(proposal.proposal_id, proposal)
    model = build_dashboard(store, clock)
    html = render_dashboard(model)
    assert f"<code>VETO {proposal.proposal_id}</code>" in html
    # the command text must not be wrapped in an anchor or button
    assert re.search(rf"<a[^>]*>[^<]*VETO {re.escape(proposal.proposal_id)}", html) is None
    assert re.search(rf"<button[^>]*>[^<]*VETO {re.escape(proposal.proposal_id)}", html) is None


# -- render_evaluation ------------------------------------------------------------------------------------------


def test_render_evaluation_shows_reasoner_sources_and_not_proposed() -> None:
    html = render_evaluation(
        {
            "cycle_date": "2026-10-19",
            "summary": "weekly check-in",
            "reasoner": {"summary": "holding steady", "holdings": [{"ticker": "AAPL", "stance": "hold", "thesis_status": "intact"}]},
            "sources": ["https://example.com/a"],
            "ideas_not_proposed": [{"priority": 1, "reason": "unpriced", "detail": "ZZZZ"}],
        }
    )
    assert "weekly check-in" in html
    assert "holding steady" in html
    assert "AAPL" in html
    assert "https://example.com/a" in html
    assert "unpriced" in html
    assert "<script" not in html.lower()


def test_render_evaluation_escapes_untrusted_fields() -> None:
    html = render_evaluation({"summary": "<script>alert(1)</script>"})
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


# -- write_dashboard --------------------------------------------------------------------------------------------


def test_write_dashboard_creates_dir_and_files_atomically(tmp_path: Path) -> None:
    model = _model(tmp_path)
    written = write_dashboard(model, tmp_path / "ws", render_dashboard, {})
    assert (tmp_path / "ws" / "dashboard" / "portfolio.html").exists()
    assert (tmp_path / "ws" / "dashboard" / "portfolio.json").exists()
    assert len(written) == 2
    for path in written:
        assert not path.with_name(path.name + ".tmp").exists()


def test_write_dashboard_sidecar_json_is_valid_and_rejects_nan(tmp_path: Path) -> None:
    model = _model(tmp_path)
    write_dashboard(model, tmp_path / "ws", render_dashboard, {})
    text = (tmp_path / "ws" / "dashboard" / "portfolio.json").read_text()
    data = json.loads(text)  # must parse
    assert "nan" not in text.lower()
    assert data["nav"] is not None


def test_write_dashboard_writes_eval_pages(tmp_path: Path) -> None:
    model = _model(tmp_path)
    written = write_dashboard(model, tmp_path / "ws", render_dashboard, {"eval-2026-10-19.html": "<p>hi</p>"})
    eval_path = tmp_path / "ws" / "dashboard" / "evaluations" / "eval-2026-10-19.html"
    assert eval_path.exists()
    assert eval_path in written


def test_write_dashboard_rejects_unsafe_eval_page_name(tmp_path: Path) -> None:
    model = _model(tmp_path)
    with pytest.raises(ValueError):
        write_dashboard(model, tmp_path / "ws", render_dashboard, {"../escape.html": "x"})


def test_model_as_dict_is_json_serializable(tmp_path: Path) -> None:
    model = _model(tmp_path)
    data = model_as_dict(model)
    json.dumps(data, default=str)  # must not raise


# -- SVG charts -------------------------------------------------------------------------------------------------


def test_svg_line_chart_has_axes_points_and_tooltips() -> None:
    points = [NAVPoint(date=T0.date(), value=Decimal(100)), NAVPoint(date=TODAY.date(), value=Decimal(110))]
    svg = svg_line_chart([("Portfolio", points)], "NAV")
    assert svg.startswith("<svg")
    assert "<path" in svg
    assert "<circle" in svg
    assert "<title>" in svg


def test_svg_line_chart_handles_empty_series() -> None:
    svg = svg_line_chart([], "NAV")
    assert svg.startswith("<svg")


def test_svg_donut_chart_has_slices_and_legend() -> None:
    sectors = [SectorWeight(sector="Tech", weight_pct=Decimal(60), tickers=["AAPL"]), SectorWeight(sector="Cash", weight_pct=Decimal(40))]
    svg = svg_donut_chart(sectors, "Sectors")
    assert "<svg" in svg
    assert "<path" in svg
    assert '<ul class="legend">' in svg
    assert "Tech" in svg and "Cash" in svg


# -- Security: DASH-20..26 ----------------------------------------------------------------------------------------


def test_dash20_no_js_tags(tmp_path: Path) -> None:
    html = render_dashboard(_model(tmp_path))
    assert "<script" not in html.lower()


def test_dash21_csp_policy_present(tmp_path: Path) -> None:
    html = render_dashboard(_model(tmp_path))
    assert "Content-Security-Policy" in html
    assert "form-action 'none'" in html
    assert "base-uri 'none'" in html


def test_dash22_no_forms(tmp_path: Path) -> None:
    html = render_dashboard(_model(tmp_path))
    assert "<form" not in html.lower()


def test_dash23_no_iframes(tmp_path: Path) -> None:
    html = render_dashboard(_model(tmp_path))
    assert "<iframe" not in html.lower()


def test_dash24_no_event_handler_attributes(tmp_path: Path) -> None:
    html = render_dashboard(_model(tmp_path))
    assert not re.search(r"\son[a-z]+\s*=", html, re.IGNORECASE)


def test_dash25_no_javascript_urls(tmp_path: Path) -> None:
    html = render_dashboard(_model(tmp_path))
    assert "javascript:" not in html.lower()


def test_dash26_no_websocket_or_fetch_references(tmp_path: Path) -> None:
    html = render_dashboard(_model(tmp_path))
    for token in ("websocket", "fetch(", "xmlhttprequest", "eventsource"):
        assert token not in html.lower()


def test_write_dashboard_clears_stale_tmp_files(tmp_path: Path) -> None:
    stale = tmp_path / "ws" / "dashboard" / "portfolio.json.tmp"
    stale.parent.mkdir(parents=True)
    stale.write_text("torn")
    write_dashboard(_model(tmp_path), tmp_path / "ws", render_dashboard, {})
    assert not stale.exists()
