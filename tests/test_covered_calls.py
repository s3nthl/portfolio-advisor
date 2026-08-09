"""Covered-call advisor tests against the fixture book + a synthetic chain."""
from __future__ import annotations

import pytest

from analytics import recommend_covered_calls
from ingest.fixture import load_fixture_book


def _call(strike, bid, delta, dte=35):
    return {"strike": strike, "bid": bid, "ask": bid + 0.1,
            "mark": bid + 0.05, "delta": delta, "oi": 500, "volume": 100, "dte": dte}


def _chain(spot, calls, dte=35):
    return {"underlying": spot,
            "expirations": [{"expiry": "2026-09-05", "dte": dte, "calls": calls}]}


@pytest.fixture
def result():
    book = load_fixture_book()
    # NVDA cost 95, mark 118; strikes incl one BELOW cost (90) that must be ignored.
    chains = {
        "NVDA": _chain(118.0, [
            _call(90, 30.0, 0.80),   # below cost + ITM -> must be excluded
            _call(120, 3.00, 0.33),
            _call(125, 2.20, 0.25),
            _call(130, 1.50, 0.20),
            _call(140, 0.80, 0.13),
        ]),
    }
    return recommend_covered_calls(book, chains)


def _rec(result, sym):
    return next(r for r in result["recommendations"] if r["symbol"] == sym)


def test_nvda_contracts_net_of_existing_coverage(result):
    nvda = _rec(result, "NVDA")
    # 1000 shares = 10 lots, 3 already covered (130 call x3) -> 7 available
    assert nvda["lots"] == 10 and nvda["covered_lots"] == 3 and nvda["contracts"] == 7
    assert nvda["status"] == "ok"


def test_never_recommends_below_cost_basis(result):
    nvda = _rec(result, "NVDA")
    for p in nvda["picks"]:
        assert p["strike"] >= nvda["cost_basis"]   # 95
    assert all(p["strike"] != 90 for p in nvda["picks"])


def test_if_called_return_always_positive(result):
    # strike >= cost + premium > 0  =>  guaranteed profit if assigned
    nvda = _rec(result, "NVDA")
    for p in nvda["picks"]:
        assert p["if_called_return_pct"] > 0


def test_premium_is_bid_and_totals_scale_by_contracts(result):
    nvda = _rec(result, "NVDA")
    p = next(p for p in nvda["picks"] if p["strike"] == 130)
    assert p["premium"] == 1.50
    assert p["total_premium"] == round(1.50 * 100 * 7, 2)   # 1,050
    assert p["assignment_prob_pct"] == 20.0                  # delta 0.20


def test_delta_tiers_selected(result):
    nvda = _rec(result, "NVDA")
    strikes = {p["strike"] for p in nvda["picks"]}
    # conservative~0.15->140, balanced~0.20->130, aggressive~0.30->120
    assert {120, 130, 140} <= strikes


def test_fully_covered_and_small_positions():
    book = load_fixture_book()
    res = recommend_covered_calls(book, {})   # no chains
    meta = _rec(res, "AAPL")
    assert meta["status"] == "no_chain"        # 800 sh eligible but no chain given
    # META has 200 sh = 2 lots -> eligible; a 30-share name would be skipped
    assert all(r["lots"] >= 1 for r in res["recommendations"])


def test_summary_totals(result):
    assert result["summary"]["actionable"] >= 1
    assert result["summary"]["total_premium_balanced"] > 0


def test_income_left_range_conservative_to_balanced(result):
    nvda = _rec(result, "NVDA")
    il = nvda["income_left"]
    # Conservative (K140 $0.80) low, Balanced (K130 $1.50) high, x7 contracts
    assert il["low"] == round(0.80 * 100 * 7, 2)    # 560
    assert il["high"] == round(1.50 * 100 * 7, 2)   # 1,050
    assert il["low"] <= il["high"]
    assert il["annual_high"] >= il["high"]           # annualized >= single cycle
    # portfolio roll-up present
    s = result["summary"]
    assert s["income_left_low"] <= s["income_left_high"]


def test_near_money_flag(result):
    # Aggressive tier (delta 0.35 -> K120) is near-the-money; conservative is not.
    nvda = _rec(result, "NVDA")
    by = {p["label"]: p for p in nvda["picks"]}
    assert by["Aggressive"]["near_money"] is True     # delta 0.35 >= 0.30
    assert by["Conservative"]["near_money"] is False  # delta 0.12
