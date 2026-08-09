"""Offline inline fixture — the frozen Jul-31 known-answer Book.

Two non-runtime jobs (never a substitute for the live API):
  1. Phase-2 regression: this hand-built Book reproduces the spec's Jul-31
     numbers exactly (see tests/), so the engine can be tested without a live
     account.
  2. Degraded-mode fallback: if the live pull fails, the app can show the last
     good snapshot instead of nothing.

Numbers were reverse-engineered from the spec targets:
  Stock MV 863,056 | Cash 239,898 | LEAP MV 89,344 | CSP notional 496,800
  (14 legs) | Net Liq 1,099,690 | waterfall peak loan 256,902.
"""
from __future__ import annotations

from datetime import date

from .models import Balances, Book, OptionPos, StockPos

AS_OF = date(2026, 7, 31)

# --- Equity (market value sums to 863,056) ---
_STOCKS = [
    StockPos("NVDA", 1000, cost_basis=95.00, mark=118.00),   # 118,000
    StockPos("AAPL", 800, cost_basis=180.00, mark=220.00),   # 176,000
    StockPos("MSFT", 300, cost_basis=350.00, mark=420.00),   # 126,000
    StockPos("AMZN", 600, cost_basis=150.00, mark=185.00),   # 111,000
    StockPos("GOOGL", 700, cost_basis=140.00, mark=165.00),  # 115,500
    StockPos("META", 200, cost_basis=380.00, mark=500.00),   # 100,000
    StockPos("AMD", 600, cost_basis=160.00, mark=194.26),    # 116,556
]

# --- LEAPs: long calls > 182 DTE (market value sums to 89,344) ---
_LEAPS = [
    OptionPos("NVDA", "CALL", 5, strike=100, expiry=date(2028, 1, 21),
              trade_price=80.00, mark=100.00),   # 5*100*100 = 50,000
    OptionPos("AAPL", "CALL", 4, strike=150, expiry=date(2027, 6, 18),
              trade_price=85.00, mark=98.36),    # 4*100*98.36 = 39,344
]

# --- Covered calls: short calls, < 182 DTE, struck ABOVE cost basis ---
_COVERED_CALLS = [
    OptionPos("NVDA", "CALL", -3, strike=130, expiry=date(2026, 9, 18),
              trade_price=4.50, mark=3.20),      # 130 > cost 95
    OptionPos("AAPL", "CALL", -2, strike=235, expiry=date(2026, 9, 18),
              trade_price=5.00, mark=4.10),      # 235 > cost 180
]

# --- 14 short puts (CSPs): notional sums to 496,800 ---
_CSPS = [
    OptionPos("NVDA", "PUT", -5, strike=120, expiry=date(2026, 8, 21), trade_price=3.10, mark=2.05),
    OptionPos("NVDA", "PUT", -4, strike=115, expiry=date(2026, 9, 18), trade_price=3.40, mark=2.60),
    OptionPos("AAPL", "PUT", -2, strike=220, expiry=date(2026, 8, 21), trade_price=4.20, mark=3.00),
    OptionPos("AAPL", "PUT", -2, strike=210, expiry=date(2026, 9, 18), trade_price=3.80, mark=2.90),
    OptionPos("MSFT", "PUT", -1, strike=420, expiry=date(2026, 10, 16), trade_price=8.50, mark=6.40),
    OptionPos("MSFT", "PUT", -1, strike=400, expiry=date(2026, 11, 20), trade_price=7.90, mark=6.10),
    OptionPos("AMZN", "PUT", -2, strike=185, expiry=date(2026, 8, 21), trade_price=3.30, mark=2.20),
    OptionPos("AMZN", "PUT", -2, strike=180, expiry=date(2026, 9, 18), trade_price=3.00, mark=2.35),
    OptionPos("GOOGL", "PUT", -2, strike=165, expiry=date(2026, 10, 16), trade_price=2.80, mark=2.10),
    OptionPos("GOOGL", "PUT", -2, strike=160, expiry=date(2026, 11, 20), trade_price=2.60, mark=2.05),
    OptionPos("META", "PUT", -1, strike=500, expiry=date(2026, 12, 18), trade_price=9.50, mark=7.80),
    OptionPos("AMD", "PUT", -1, strike=120, expiry=date(2026, 9, 18), trade_price=2.40, mark=1.70),
    OptionPos("AMD", "PUT", -1, strike=118, expiry=date(2026, 10, 16), trade_price=2.30, mark=1.85),
    OptionPos("GOOGL", "PUT", -1, strike=110, expiry=date(2027, 1, 15), trade_price=2.00, mark=1.60),
]

_BALANCES = Balances(
    cash_and_sweep=239_898.00,
    option_buying_power=300_000.00,  # NOT cash
    net_liq=1_099_690.00,
    margin_loan=0.0,
    money_market=0.0,  # no SWVXX present in this snapshot
)


def load_fixture_book() -> Book:
    """Return the frozen Jul-31 known-answer Book."""
    return Book(
        stocks=list(_STOCKS),
        options=[*_LEAPS, *_COVERED_CALLS, *_CSPS],
        balances=_BALANCES,
        as_of=AS_OF,
    )
