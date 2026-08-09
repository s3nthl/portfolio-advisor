"""Phase-2 regression: the engine must reproduce the frozen Jul-31 numbers exactly.

Known-answer values (from the build spec):
    Stock MV     $863,056  (72.4% of gross)
    Cash         $239,898  (21.8% of Net Liq)
    LEAP MV      $89,344
    CSP notional $496,800  (14 legs)
    Net Liq      $1,099,690
    Waterfall peak loan (all assign)  $256,902
    VXN 28.6 -> "fear", cash 21.8% -> "in range" (target 15-25%)
"""
from __future__ import annotations

import pytest

from analytics import (
    assignment_waterfall,
    buckets,
    covered_calls_below_basis,
    margin_snapshot,
    vix_posture,
    vxn_posture,
)
from ingest.fixture import load_fixture_book


@pytest.fixture
def book():
    return load_fixture_book()


@pytest.fixture
def bk(book):
    return buckets(book)


def test_stock_market_value(bk):
    assert round(bk.stock.market_value) == 863_056


def test_stock_pct_of_gross(bk):
    assert round(bk.stock_pct_gross * 100, 1) == 72.4


def test_cash(bk):
    assert round(bk.real_cash) == 239_898


def test_cash_pct_of_net_liq(bk):
    assert round(bk.cash_pct_netliq * 100, 1) == 21.8


def test_leap_market_value(bk):
    assert round(bk.leap.market_value) == 89_344


def test_csp_notional(bk):
    assert round(bk.csp.notional) == 496_800


def test_csp_leg_count(bk):
    assert bk.csp.count == 14


def test_net_liq(bk):
    assert round(bk.net_liq) == 1_099_690


def test_waterfall_peak_loan(book):
    wf = assignment_waterfall(book)
    assert round(wf.peak_loan) == 256_902


def test_waterfall_orders_by_expiry(book):
    wf = assignment_waterfall(book)
    expiries = [s.expiry for s in wf.steps]
    assert expiries == sorted(expiries)


def test_margin_loan_starts_only_after_cash_drained(book):
    wf = assignment_waterfall(book)
    # loan may only be > 0 on steps where cash has hit 0
    for s in wf.steps:
        if s.margin_loan > 0:
            assert s.cash_remaining == 0.0


def test_vxn_band_fear_and_in_range():
    p = vxn_posture(28.6, cash_pct=0.218)
    assert p.band == "fear"
    assert (p.target_low_pct, p.target_high_pct) == (15.0, 25.0)
    assert p.status == "in range"


@pytest.mark.parametrize("vxn,band", [
    (18.0, "risk-on"), (22.0, "caution"), (28.6, "fear"),
    (32.0, "elevated"), (40.0, "panic"),
])
def test_vxn_bands(vxn, band):
    assert vxn_posture(vxn).band == band


@pytest.mark.parametrize("vix,band", [
    (14.0, "risk-on"), (16.0, "caution"), (22.0, "fear"),
    (27.0, "elevated"), (33.0, "panic"),
])
def test_vix_bands_are_calibrated_lower(vix, band):
    # VIX bands are tighter than VXN's (VIX runs structurally lower).
    assert vix_posture(vix).band == band


def test_vix_normal_makes_low_cash_below():
    # The reported bug: VIX 15.99 at 6.4% cash must read BELOW, not in range.
    p = vix_posture(15.99, cash_pct=0.064)
    assert (p.target_low_pct, p.target_high_pct) == (10.0, 15.0)
    assert p.status == "below"


def test_no_covered_calls_below_basis(book):
    # Fixture CCs are struck above cost basis; guardrail should report none.
    assert covered_calls_below_basis(book) == []


def test_pl_universes_are_separate(bk):
    # Sanity: buckets are distinct objects, never summed into one blended P&L.
    names = {bk.stock.name, bk.csp.name, bk.covered_call.name, bk.leap.name}
    assert names == {"Stock", "CSP", "Covered Call", "LEAPs/Calls"}


def test_margin_snapshot_matches_waterfall(book):
    ms = margin_snapshot(book)
    assert round(ms.loan_if_all_assigned) == 256_902
    assert ms.margin_loan == 0.0
