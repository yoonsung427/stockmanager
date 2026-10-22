#!/usr/bin/env python3
"""Import positions from a CSV file with columns: Symbol, Name, Quantity.

Creates synthetic BUY trades that derive Position objects via apply_fill.
Skips symbols already present in the portfolio.
Usage:
    python import_positions_from_csv.py /path/to/portfolio.csv
"""
import csv
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from zoneinfo import ZoneInfo

from stockmanager.app import create_app
from stockmanager.domain.docs import PortfolioDoc, Trade
from stockmanager.domain.fills import LedgerState, apply_fill
from stockmanager.domain.money import q4
from stockmanager.pricing.service import PriceService


PT = ZoneInfo("America/Los_Angeles")


def parse_quantity(raw: str) -> Decimal:
    """Parse quantity string, stripping commas and whitespace."""
    return Decimal(raw.replace(",", "").strip())


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python import_positions_from_csv.py <csv_file>")
        sys.exit(1)

    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        print(f"File not found: {csv_path}")
        sys.exit(1)

    ctx = create_app(Path("workspace"))
    portfolio = ctx.store.load_portfolio()
    if portfolio is None:
        # No portfolio yet — create one
        from datetime import datetime
        portfolio = PortfolioDoc(
            start_cash=Decimal("0"),
            cash=Decimal("0"),
            status="LIVE",
            positions=[],
            trades=[],
        )
        ctx.store.save_portfolio(portfolio)
    existing = {p.ticker for p in portfolio.positions}

    # Read CSV
    holdings: list[tuple[str, str, Decimal]] = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            symbol = row["Symbol"].strip().upper()
            name = row["Name"].strip()
            quantity = parse_quantity(row["Quantity"])
            holdings.append((symbol, name, quantity))

    print(f"Found {len(holdings)} positions in CSV")

    # Merge duplicates within CSV
    by_symbol: dict[str, tuple[str, Decimal]] = {}
    for symbol, name, quantity in holdings:
        if symbol in by_symbol:
            old_name, old_qty = by_symbol[symbol]
            by_symbol[symbol] = (old_name, old_qty + quantity)
        else:
            by_symbol[symbol] = (name, quantity)

    # Skip symbols already in portfolio
    new_symbols = []
    skipped = []
    for symbol in sorted(by_symbol.keys()):
        if symbol in existing:
            skipped.append(symbol)
        else:
            new_symbols.append(symbol)

    if skipped:
        print(f"Skipping {len(skipped)} already-present symbols: {', '.join(skipped)}")

    if not new_symbols:
        print("All symbols already in portfolio. Nothing to import.")
        sys.exit(0)

    print(f"Importing {len(new_symbols)} new symbols: {', '.join(new_symbols)}")

    # Fetch prices
    print("Fetching prices...")
    ps = PriceService()
    groups = [[s] for s in new_symbols]
    now = datetime.now(PT)
    try:
        quotes = ps.fill_prices(groups, now.date(), ctx.price_primary, ctx.price_fallback)
    except Exception as e:
        print(f"Batch fetch error: {e}")
        print("Trying individual fetches...")
        quotes = {}
        for s in new_symbols:
            try:
                quote = ctx.price_primary.get_price(s, now.date(), "open")
                quotes[s] = quote
            except Exception as e2:
                print(f"  Failed {s}: {e2}")

    # Create trades
    print("Creating trades...")
    timestamp = now
    trades: list[Trade] = []
    for symbol in new_symbols:
        name, quantity = by_symbol[symbol]
        quote = quotes.get(symbol)
        if quote is None:
            print(f"  Warning: no price for {symbol}, skipping")
            continue
        cost = q4(quantity * quote.price)
        trade = Trade(
            trade_id=f"IMPORT-{uuid.uuid4().hex[:8]}",
            timestamp=timestamp,
            action="BUY",
            ticker=symbol,
            shares=q4(quantity),
            fill_price=quote.price,
            cost=cost,
            price_source=quote.source,
            thesis=f"Initial import from {csv_path.name}",
        )
        trades.append(trade)

    if not trades:
        print("No trades created (all prices failed).")
        sys.exit(1)

    # Derive positions from trades
    print("Deriving positions from trades...")
    ledger = LedgerState(
        cash=portfolio.cash,
        positions={p.ticker: p for p in portfolio.positions},
    )
    for trade in trades:
        ledger = apply_fill(ledger, trade)

    # Update portfolio
    new_portfolio = portfolio.model_copy(
        update={
            "cash": ledger.cash,
            "positions": list(ledger.positions.values()),
            "trades": [*portfolio.trades, *trades],
        }
    )

    print(f"Adding {len(trades)} trades ({len(ledger.positions)} positions total)")
    print(f"Cash balance: {new_portfolio.cash}")

    confirm = "y"  # auto-confirm for CLI usage
    # confirm = input("Confirm import? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        sys.exit(0)

    ctx.store.save_portfolio(new_portfolio)
    ctx.store.append_trades_log(
        [
            f"{trade.trade_id}\t{trade.timestamp.isoformat()}\t{trade.action}\t{trade.ticker}\t{trade.shares}\t{trade.fill_price}\t{trade.cost}\t{trade.price_source}"
            for trade in trades
        ]
    )

    print("Import complete.")


if __name__ == "__main__":
    main()
