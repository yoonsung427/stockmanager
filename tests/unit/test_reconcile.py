"""Reconciliation: rebuilding positions, divergence detection, log suffix repair (AS §1.6)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from stockmanager.store.reconcile import (
    InvalidHistory,
    ReconcileError,
    compare_log,
    format_log_line,
    rebuild_positions,
    reconcile_portfolio,
    repair_log_suffix,
)
from tests.support.books import make_portfolio, make_trade


def test_rebuild_single_buy() -> None:
    (p,) = rebuild_positions([make_trade(1, shares="53.376", price="187.3")])
    assert p.ticker == "AAPL"
    assert p.shares == Decimal("53.3760")
    assert p.cost_basis == Decimal("9997.3248")
    assert p.fill_price == Decimal("187.3000")
    assert p.thesis == "t"


def test_rebuild_adds_use_average_cost_first_fill_time_latest_thesis() -> None:
    trades = [make_trade(1, shares="10", price="100", thesis="a"), make_trade(2, shares="10", price="200", thesis="b")]
    (p,) = rebuild_positions(trades)
    assert p.shares == Decimal("20.0000")
    assert p.cost_basis == Decimal("3000.0000")
    assert p.fill_price == Decimal("150.0000")
    assert p.fill_time == trades[0].timestamp
    assert p.thesis == "b"


def test_rebuild_partial_sell_reduces_basis_proportionally() -> None:
    (p,) = rebuild_positions([make_trade(1, shares="10", price="100"), make_trade(2, "SELL", shares="4", price="130")])
    assert p.shares == Decimal("6.0000")
    assert p.cost_basis == Decimal("600.0000")
    assert p.fill_price == Decimal("100.0000")


def test_rebuild_full_sell_drops_position() -> None:
    assert rebuild_positions([make_trade(1), make_trade(2, "SELL")]) == []


def test_rebuild_oversell_is_invalid_history() -> None:
    with pytest.raises(InvalidHistory) as e:
        rebuild_positions([make_trade(1, shares="1"), make_trade(2, "SELL", shares="2")])
    assert e.value.trade_id.endswith("sell2")


def test_rebuild_sell_without_position_is_invalid() -> None:
    with pytest.raises(InvalidHistory):
        rebuild_positions([make_trade(1, "SELL")])


def test_negative_cash_is_invalid_history() -> None:
    doc = make_portfolio([make_trade(1, shares="1", price="100")], start_cash="100")
    broken = doc.model_copy(update={"start_cash": Decimal(50)})
    with pytest.raises(InvalidHistory, match="negative cash"):
        reconcile_portfolio(broken)


def test_reconcile_clean_portfolio_has_no_divergence() -> None:
    doc = make_portfolio([make_trade(1), make_trade(2, ticker="MSFT", price="50")])
    rebuilt, divs = reconcile_portfolio(doc)
    assert divs == []
    assert rebuilt == doc


def test_reconcile_tolerance_boundary() -> None:
    doc = make_portfolio([make_trade(1)])
    ok = doc.model_copy(update={"cash": doc.cash + Decimal("0.0001")})
    assert reconcile_portfolio(ok)[1] == []
    bad = doc.model_copy(update={"cash": doc.cash + Decimal("0.0002")})
    assert any(d.startswith("cash:") for d in reconcile_portfolio(bad)[1])


def test_reconcile_reports_and_repairs_position_drift_without_touching_trades() -> None:
    doc = make_portfolio([make_trade(1)])
    drifted = doc.model_copy(update={"positions": [doc.positions[0].model_copy(update={"shares": Decimal("11.0000")})]})
    rebuilt, divs = reconcile_portfolio(drifted)
    assert any("shares stored 11.0000, derived 10.0000" in d for d in divs)
    assert rebuilt.positions[0].shares == Decimal("10.0000")
    assert rebuilt.trades == doc.trades


def test_reconcile_extra_and_missing_tickers() -> None:
    doc = make_portfolio([make_trade(1)])
    _, divs = reconcile_portfolio(doc.model_copy(update={"positions": []}))
    assert any("AAPL: missing from positions" in d for d in divs)
    ghost = doc.positions[0].model_copy(update={"ticker": "ZZZ"})
    _, divs = reconcile_portfolio(doc.model_copy(update={"positions": [*doc.positions, ghost]}))
    assert any("ZZZ: in positions" in d for d in divs)


def test_reconcile_keeps_edited_thesis() -> None:
    doc = make_portfolio([make_trade(1)])
    edited = doc.model_copy(update={"positions": [doc.positions[0].model_copy(update={"thesis": "mine"})]})
    rebuilt, divs = reconcile_portfolio(edited)
    assert divs == []
    assert rebuilt.positions[0].thesis == "mine"


# -- log comparison -----------------------------------------------------------------------------------------------


def test_format_log_line_matches_spec_shape() -> None:
    line = format_log_line(make_trade(1, shares="10", price="195"))
    assert line == (
        "P-OPEN-2026-10-12-buy1 | 2026-10-12T06:31:00-07:00 | BUY | AAPL | 10.0000 | 195.0000 | 1950.0000 | yahoo "
        "| mock fill, no real money moved."
    )


def test_compare_log_clean() -> None:
    trades = [make_trade(1), make_trade(2)]
    res = compare_log(trades, [format_log_line(t) for t in trades])
    assert res.missing_suffix == [] and res.divergences == []


def test_compare_log_missing_suffix_is_repairable() -> None:
    trades = [make_trade(1), make_trade(2), make_trade(3)]
    res = compare_log(trades, [format_log_line(trades[0])])
    assert res.missing_suffix == [trades[1].trade_id, trades[2].trade_id]
    assert res.divergences == []


def test_compare_log_missing_non_suffix_diverges() -> None:
    trades = [make_trade(1), make_trade(2), make_trade(3)]
    res = compare_log(trades, [format_log_line(trades[0]), format_log_line(trades[2])])
    assert res.missing_suffix == []
    assert any("not a suffix" in d for d in res.divergences)


def test_compare_log_detects_torn_extra_duplicate_and_field_mismatch() -> None:
    t1, t2 = make_trade(1), make_trade(2)
    line1 = format_log_line(t1)
    res = compare_log([t1], [line1[:30]])
    assert any("unparsable or torn" in d for d in res.divergences)
    res = compare_log([t1], [line1, format_log_line(t2)])
    assert any("has no trade" in d for d in res.divergences)
    res = compare_log([t1], [line1, line1])
    assert any("duplicate" in d for d in res.divergences)
    res = compare_log([t1], [line1.replace("10.0000", "11.0000", 1)])
    assert any("shares differs" in d for d in res.divergences)


# -- suffix repair ------------------------------------------------------------------------------------------------


def test_repair_log_suffix_appends_only_missing(tmp_path: Path) -> None:
    trades = [make_trade(1), make_trade(2), make_trade(3)]
    expected = [format_log_line(t) for t in trades]
    log = tmp_path / "trades.log"
    log.write_text(expected[0] + "\n", encoding="utf-8")
    assert repair_log_suffix(log, expected) == expected[1:]
    assert log.read_text(encoding="utf-8") == "\n".join(expected) + "\n"
    assert repair_log_suffix(log, expected) == []


def test_repair_log_suffix_creates_missing_file(tmp_path: Path) -> None:
    expected = [format_log_line(make_trade(1))]
    log = tmp_path / "trades.log"
    repair_log_suffix(log, expected)
    assert log.read_text(encoding="utf-8") == expected[0] + "\n"


def test_repair_log_suffix_refuses_torn_line_and_leaves_file_alone(tmp_path: Path) -> None:
    expected = [format_log_line(make_trade(1)), format_log_line(make_trade(2))]
    log = tmp_path / "trades.log"
    log.write_text(expected[0][:25], encoding="utf-8")
    with pytest.raises(ReconcileError):
        repair_log_suffix(log, expected)
    assert log.read_text(encoding="utf-8") == expected[0][:25]


def test_repair_log_suffix_refuses_extra_line(tmp_path: Path) -> None:
    expected = [format_log_line(make_trade(1))]
    log = tmp_path / "trades.log"
    log.write_text(expected[0] + "\nrogue\n", encoding="utf-8")
    with pytest.raises(ReconcileError):
        repair_log_suffix(log, expected)
