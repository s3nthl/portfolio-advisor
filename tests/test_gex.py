"""GEX methodology regression — pure math over a synthetic chain (no I/O)."""
from __future__ import annotations

import math

from analytics.gex import (_bs_gamma, _dollar_gamma, bias_read, chain_total_oi,
                           compute_gex)


def test_bs_gamma_known_value():
    # S=K=100, T=1y, sigma=20%, r=4%: d1=0.3, gamma = pdf(0.3)/(100*0.2)
    g = _bs_gamma(100, 100, 1.0, 0.20, r=0.04)
    assert math.isclose(g, 0.0190694, rel_tol=1e-4)


def test_bs_gamma_degenerate():
    assert _bs_gamma(100, 100, 0, 0.2) == 0.0      # no time
    assert _bs_gamma(100, 100, 1, 0) == 0.0        # no vol
    assert _bs_gamma(0, 100, 1, 0.2) == 0.0        # no spot


def test_dollar_gamma():
    # gamma*oi*100*S^2*0.01 = 0.02*1000*100*10000*0.01
    assert _dollar_gamma(0.02, 1000, 100) == 200_000.0


def _chain():
    return {
        "underlying": 100.0,
        "expirations": [{
            "expiry": "2026-09-18", "dte": 30,
            "calls": [
                {"strike": 105, "oi": 2000, "gamma": 0.02, "iv": 0.2, "dte": 30},
                {"strike": 110, "oi": 500, "gamma": 0.01, "iv": 0.2, "dte": 30},
                {"strike": 130, "oi": 9999, "gamma": 0.05, "iv": 0.2, "dte": 30},  # >20% OTM: excluded
            ],
            "puts": [
                {"strike": 95, "oi": 1000, "gamma": 0.02, "iv": 0.2, "dte": 30},
                {"strike": 90, "oi": 500, "gamma": 0.01, "iv": 0.2, "dte": 30},
            ],
        }],
    }


def test_compute_gex_totals_and_walls():
    r = compute_gex(_chain(), "TEST")
    assert r.spot == 100.0
    # call GEX: 0.02*2000*1e4 + 0.01*500*1e4 = 400k + 50k = 450k
    assert r.call_gex_total == 450_000.0
    # put GEX (dealer-signed, negative): -(0.02*1000*1e4 + 0.01*500*1e4) = -250k
    assert r.put_gex_total == -250_000.0
    assert r.net_gex == 200_000.0            # 450k - 250k
    assert r.regime == "positive"
    assert r.call_wall == 105                # 400k is the biggest call strike
    assert r.put_wall == 95                  # 200k is the biggest put strike
    assert r.contracts == 4                  # the 130 strike was band-excluded


def test_band_excludes_far_otm():
    r = compute_gex(_chain(), "TEST")
    strikes = {s["strike"] for s in r.strikes}
    assert 130 not in strikes                # 30% OTM dropped
    assert strikes == {90, 95, 105, 110}


def test_expiry_filter():
    chain = _chain()
    chain["expirations"].append({
        "expiry": "2026-12-18", "dte": 120,
        "calls": [{"strike": 108, "oi": 3000, "gamma": 0.03, "iv": 0.25, "dte": 120}],
        "puts": [],
    })
    both = compute_gex(chain, "TEST")
    assert len(both.expiries_available) == 2
    one = compute_gex(chain, "TEST", expiries=["2026-12-18"])
    assert one.expiries_used == ["2026-12-18"]
    assert one.call_wall == 108              # only the Dec strike remains
    assert one.contracts == 1


def test_flip_between_walls():
    # Puts below spot, calls above: net gamma is negative below / positive above,
    # so the zero-gamma flip should sit near spot (~100).
    chain = {
        "underlying": 100.0,
        "expirations": [{
            "expiry": "2026-09-18", "dte": 30,
            "calls": [{"strike": 105, "oi": 1000, "gamma": 0.02, "iv": 0.2, "dte": 30}],
            "puts": [{"strike": 95, "oi": 1000, "gamma": 0.02, "iv": 0.2, "dte": 30}],
        }],
    }
    r = compute_gex(chain, "SYM")
    assert r.flip is not None
    assert 90 <= r.flip <= 110               # crossing lands within the scan band


def test_walls_constrained_to_correct_side_of_spot():
    # A big call-gamma strike BELOW spot must NOT become the call wall (resistance
    # is overhead); likewise a huge put strike above spot isn't the put wall.
    chain = {
        "underlying": 100.0,
        "expirations": [{
            "expiry": "2026-09-18", "dte": 30,
            "calls": [
                {"strike": 90, "oi": 9000, "gamma": 0.05, "iv": 0.2, "dte": 30},   # below spot: ignore for call wall
                {"strike": 105, "oi": 2000, "gamma": 0.02, "iv": 0.2, "dte": 30},  # above spot: THE call wall
            ],
            "puts": [
                {"strike": 110, "oi": 9000, "gamma": 0.05, "iv": 0.2, "dte": 30},  # above spot: ignore for put wall
                {"strike": 95, "oi": 2000, "gamma": 0.02, "iv": 0.2, "dte": 30},   # below spot: THE put wall
            ],
        }],
    }
    r = compute_gex(chain, "TEST")
    assert r.call_wall == 105    # not 90, even though 90 has far more call gamma
    assert r.put_wall == 95      # not 110


def test_wall_weak_oi_flag_and_total():
    chain = {
        "underlying": 100.0,
        "expirations": [{
            "expiry": "2026-09-18", "dte": 30,
            "calls": [{"strike": 105, "oi": 50, "gamma": 0.02, "iv": 0.2, "dte": 30}],   # OI 50 < WEAK_OI
            "puts": [{"strike": 95, "oi": 5000, "gamma": 0.02, "iv": 0.2, "dte": 30}],   # OI 5000 strong
        }],
    }
    r = compute_gex(chain, "THIN")
    assert r.call_wall_weak is True     # thin call wall flagged
    assert r.put_wall_weak is False
    assert r.call_wall_oi == 50 and r.put_wall_oi == 5000
    assert r.total_oi == 5050
    assert chain_total_oi(chain) == 5050


def test_bias_stabilizing_and_amplifying():
    up = compute_gex({"underlying": 100.0, "expirations": [{"expiry": "2026-09-18", "dte": 30,
        "calls": [{"strike": 105, "oi": 3000, "gamma": 0.03, "iv": 0.2, "dte": 30}],
        "puts": [{"strike": 95, "oi": 1000, "gamma": 0.02, "iv": 0.2, "dte": 30}]}]}, "SYM")
    b = bias_read(up)
    assert b["regime"] in ("STABILIZING", "AMPLIFYING")
    assert b["color"] in ("green", "red", "amber")
    # spot above flip -> stabilizing, trigger phrased as flips bearish below
    if up.flip is not None and up.spot > up.flip:
        assert b["regime"] == "STABILIZING" and b["color"] == "green"
        assert "bearish below" in b["trigger"]


def test_bias_none_without_spot():
    r = compute_gex({"underlying": 0, "expirations": []}, "SYM")
    assert bias_read(r) is None


def test_flat_net_has_no_flip():
    # Matched call/put gamma at the same strike -> net identically zero -> no flip
    # (must NOT return the low scan bound).
    chain = {
        "underlying": 100.0,
        "expirations": [{
            "expiry": "2026-09-18", "dte": 30,
            "calls": [{"strike": 100, "oi": 1000, "gamma": 0.03, "iv": 0.2, "dte": 30}],
            "puts": [{"strike": 100, "oi": 1000, "gamma": 0.03, "iv": 0.2, "dte": 30}],
        }],
    }
    assert compute_gex(chain, "SYM").flip is None
