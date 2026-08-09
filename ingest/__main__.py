"""Print the normalized Book to the console.

    python -m ingest                # uses CHAI_SOURCE (fixture by default)
    CHAI_SOURCE=schwab python -m ingest    # live read-only pull

Phase-1 gate helper: proves the fixture loads offline, and (once the user has
put Schwab creds in .env and run the OAuth flow) that the real book prints.
"""
from __future__ import annotations

import sys

import config

from . import load_book
from .models import Book


def _fmt(x: float) -> str:
    return f"${x:>14,.2f}"


def print_book(book: Book) -> None:
    today = book.as_of
    print(f"\n=== ChaiStreet Command — Book (source={config.CHAI_SOURCE}, as_of={today}) ===\n")

    print(f"STOCKS ({len(book.stocks)})")
    stock_mv = 0.0
    for s in sorted(book.stocks, key=lambda p: -p.market_value):
        stock_mv += s.market_value
        print(f"  {s.symbol:6} qty {s.qty:>8.0f}  mark {s.mark:>9.2f}  "
              f"MV {_fmt(s.market_value)}  P/L {_fmt(s.pl_open)}")
    print(f"  {'':6} stock market value: {_fmt(stock_mv)}\n")

    print(f"OPTIONS ({len(book.options)})")
    for o in sorted(book.options, key=lambda p: (p.kind, p.symbol, p.expiry)):
        side = "SHORT" if o.is_short else "LONG "
        print(f"  {side} {o.kind:4} {o.symbol:6} {o.strike:>8.2f} "
              f"exp {o.expiry} ({o.dte(today):>4}d) qty {o.qty:>4.0f}  "
              f"mark {o.mark:>8.2f}  notional {_fmt(o.notional)}")
    print()

    b = book.balances
    print("BALANCES")
    print(f"  Cash & Sweep      {_fmt(b.cash_and_sweep)}")
    print(f"  Money market      {_fmt(b.money_market)}")
    print(f"  Real cash         {_fmt(b.real_cash)}")
    print(f"  Option BP (n/cash){_fmt(b.option_buying_power)}")
    print(f"  Margin loan       {_fmt(b.margin_loan)}")
    print(f"  Net Liq           {_fmt(b.net_liq)}")
    print()


def main() -> int:
    try:
        book = load_book()
    except Exception as exc:
        print(f"Failed to load book: {exc}", file=sys.stderr)
        return 1
    print_book(book)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
