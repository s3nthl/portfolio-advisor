"""Sector-analysis tests against the frozen Jul-31 fixture.

Covers ALL positions, aggregated by underlying, with the owner SECTOR_OVERRIDE
taxonomy (no live Finnhub in tests -> override + static fallback).

Exposure EXCLUDES covered-call notional (CCs cap upside on shares already
counted — including their notional would double-count). Fixture underlyings:
    NVDA -> Semiconductors  stock 118k + csp 106k + LEAP 50k = 274,000  (cc 39k excluded)
    AMD  -> Semiconductors  stock 116,556 + csp 23,800       = 140,356
    AAPL/MSFT/AMZN/GOOGL/META -> Big Tech (override)
  Big Tech       = 1,034,844
  Semiconductors =   414,356
  gross          = 1,449,200
"""
from __future__ import annotations

import pytest

from analytics import sector_analysis
from analytics.reference import HIGH_BETA, resolve
from ingest.fixture import load_fixture_book


@pytest.fixture
def sa():
    return sector_analysis(load_fixture_book())


def test_gross_exposure(sa):
    assert round(sa["gross_exposure"]) == 1_449_200


def test_sector_values_sum_to_gross(sa):
    assert round(sum(g["value"] for g in sa["sectors"])) == 1_449_200


def test_override_taxonomy_sectors(sa):
    assert {g["sector"] for g in sa["sectors"]} == {"Big Tech", "Semiconductors"}


def test_big_tech_is_largest(sa):
    assert sa["sectors"][0]["sector"] == "Big Tech"
    assert round(sa["sectors"][0]["value"]) == 1_034_844


def test_high_beta_exposure(sa):
    assert round(sa["high_beta_value"]) == 414_356
    assert sa["high_beta_threshold"] == HIGH_BETA
    hb = {h["symbol"] for g in sa["sectors"] for h in g["holdings"] if h["high_beta"]}
    assert hb == {"NVDA", "AMD"}


def test_covered_calls_excluded_from_exposure(sa):
    nvda = next(h for g in sa["sectors"] for h in g["holdings"] if h["symbol"] == "NVDA")
    assert nvda["sector"] == "Semiconductors"          # override wins
    assert round(nvda["stock"]) == 118_000
    assert round(nvda["csp"]) == 106_000
    assert round(nvda["call"]) == 50_000
    assert round(nvda["exposure"]) == 274_000          # NO covered-call notional
    # covered call kept as info only
    assert nvda["capped"] is True
    assert nvda["cc_contracts"] == 3
    assert round(nvda["cc_notional"]) == 39_000


def test_no_leveraged_in_fixture(sa):
    assert sa["leveraged_value"] == 0.0


def test_per_sector_holding_pct_sums_to_100(sa):
    for g in sa["sectors"]:
        assert round(sum(h["pct_of_sector"] for h in g["holdings"])) == 100


# --- resolve() priority ---
def test_resolve_override_beats_live_sector():
    sector, beta, lev = resolve("NVDA", live_sector="Technology", live_beta=2.0)
    assert sector == "Semiconductors"   # override wins over Finnhub
    assert beta == 2.0                  # live beta wins over static
    assert lev is False


def test_resolve_live_sector_when_no_override():
    # AMD has no override; a live sector should win over the static one.
    sector, beta, lev = resolve("AMD", live_sector="Custom Semis", live_beta=1.9)
    assert sector == "Custom Semis"
    assert beta == 1.9


def test_resolve_leveraged_flag_keeps_sector():
    sector, beta, lev = resolve("SOXL")
    assert sector == "Semiconductors"   # LEV is a flag, not a sector
    assert lev is True


def test_resolve_unknown():
    assert resolve("ZZZZ") == ("Unclassified", None, False)
