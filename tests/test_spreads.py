"""Vertical-spread detection & risk math, plus the guarantee that a spread's
short leg is NOT double-counted as a naked cash-secured put / covered call.
"""
from datetime import date

from ingest.models import Balances, Book, OptionPos, detect_spreads


def _opt(kind, qty, strike, trade, mark, exp="2026-11-20"):
    return OptionPos(symbol="SPY", kind=kind, qty=qty, strike=strike,
                     expiry=date.fromisoformat(exp), trade_price=trade, mark=mark)


def _book(options):
    return Book(stocks=[], options=options,
                balances=Balances(cash_and_sweep=0, option_buying_power=0, net_liq=100000),
                as_of=date(2026, 8, 5))


def test_bear_put_debit_spread_math():
    # long 770 put / short 750 put x10 — the user's SPY position
    longp = _opt("PUT", 10, 770, 21.62, 21.68)
    shortp = _opt("PUT", -10, 750, 15.53, 15.575)
    spreads, legs = detect_spreads([longp, shortp])
    assert len(spreads) == 1
    s = spreads[0]
    assert s.direction == "bear" and s.kind == "PUT" and s.is_debit
    assert s.width == 20.0
    assert s.contracts == 10
    assert s.max_loss == round(6.09 * 100 * 10, 2)          # the net debit
    assert s.max_profit == round((20 - 6.09) * 100 * 10, 2)
    assert s.breakeven == 763.91
    assert id(longp) in legs and id(shortp) in legs


def test_bull_call_debit_spread_math():
    # long 100 call / short 115 call x7 — the INTC position
    longc = _opt("CALL", 7, 100, 8.0, 8.2)
    shortc = _opt("CALL", -7, 115, 2.643, 2.6)
    spreads, _ = detect_spreads([longc, shortc])
    s = spreads[0]
    assert s.direction == "bull" and s.is_debit and s.width == 15.0
    assert s.max_loss == round((8.0 - 2.643) * 100 * 7, 2)
    assert s.breakeven == round(100 + (8.0 - 2.643), 2)


def test_spread_legs_excluded_from_naked_selectors():
    longp = _opt("PUT", 10, 770, 21.62, 21.68)
    shortp = _opt("PUT", -10, 750, 15.53, 15.575)      # hedged -> NOT a CSP
    naked = _opt("PUT", -1, 300, 5.0, 4.0, exp="2026-09-18")  # a real naked CSP
    b = _book([longp, shortp, naked])
    csp_syms = [(o.symbol, o.strike) for o in b.short_puts]
    assert (300.0,) not in csp_syms and any(st == 300 for _, st in csp_syms)
    # the spread's short 750 put must NOT appear as a naked CSP
    assert all(st != 750 for _, st in csp_syms)
    assert len(b.spreads) == 1


def test_credit_spread_risk_is_width_minus_credit():
    # short 300 put / long 290 put -> bull put CREDIT spread
    shortp = _opt("PUT", -5, 300, 6.0, 5.0)
    longp = _opt("PUT", 5, 290, 3.0, 2.5)
    s = detect_spreads([shortp, longp])[0][0]
    assert s.direction == "bull" and not s.is_debit
    assert s.max_profit == round(3.0 * 100 * 5, 2)                 # the credit
    assert s.max_loss == round((10 - 3.0) * 100 * 5, 2)           # width - credit


def test_same_strike_is_not_a_spread():
    # same strike long+short = not a vertical -> left as naked legs
    a = _opt("PUT", 5, 300, 6.0, 5.0)
    b = _opt("PUT", -5, 300, 6.0, 5.0)
    spreads, legs = detect_spreads([a, b])
    assert spreads == [] and legs == set()
