"""Screener scoring regression tests.

The critical guarantee: percentile direction is correct — higher-is-better metrics
(ROE, margins, growth) rank the strong company up; lower-is-better metrics (P/E,
debt) rank the cheap/safe company up. A sign flip here silently inverts every
ranking (it did, once — this test exists so it can't again).
"""
from analytics.screener import run_screen


def _fin(roe, net_margin, gross_margin, op_inc, rev, ni, eps, shares, ocf, de, years=8):
    """Minimal Polygon-shaped financials for one ticker."""
    def series(v):
        return {"data": [{"label": f"y{i}", "v": v} for i in range(years)]}
    return {
        "metrics": {
            "Return on Equity": series(roe),
            "Net Margin": series(net_margin),
            "Gross Margin": series(gross_margin),
            "Operating Income": series(op_inc),
            "Revenue": series(rev),
            "Net Income": series(ni),
            "EPS (diluted)": series(eps),
            "Cash from Operations": series(ocf),
            "Debt / Equity": series(de),
            "Shareholder Equity": series(rev * 0.5),
        },
        "basis": [{"date": f"20{18+i}-12-31", "eps": eps, "revenue": rev, "shares": shares}
                  for i in range(years)],
        "count": years,
    }


def test_higher_quality_ranks_first():
    # STRONG: high ROE/margins/growth-ish, low P/E. WEAK: low everything, high P/E.
    strong = _fin(roe=40, net_margin=30, gross_margin=70, op_inc=40, rev=100, ni=30,
                  eps=10.0, shares=1e9, ocf=33, de=0.2)
    weak = _fin(roe=6, net_margin=4, gross_margin=20, op_inc=4, rev=100, ni=4,
                eps=1.0, shares=1e9, ocf=3, de=2.5)
    data = {
        "STRONG": {"fin": strong, "price": 100, "sector": "Tech"},  # P/E 10
        "WEAK": {"fin": weak, "price": 100, "sector": "Tech"},      # P/E 100
    }
    res = run_screen(data)
    sec = res["sectors"][0]
    assert sec["top"]["symbol"] == "STRONG"
    assert sec["picks"][0]["score"] > sec["picks"][1]["score"]
    assert res["global_top"][0]["symbol"] == "STRONG"


def test_cheaper_valuation_scores_higher_all_else_equal():
    base = dict(roe=25, net_margin=20, gross_margin=50, op_inc=25, rev=100, ni=20,
                eps=5.0, shares=1e9, ocf=22, de=0.5)
    data = {
        "CHEAP": {"fin": _fin(**base), "price": 50, "sector": "X"},   # P/E 10
        "PRICEY": {"fin": _fin(**base), "price": 200, "sector": "X"},  # P/E 40
    }
    res = run_screen(data)
    top = res["sectors"][0]["top"]
    assert top["symbol"] == "CHEAP"


def test_unprofitable_is_excluded_with_reason():
    loss = _fin(roe=-5, net_margin=-10, gross_margin=30, op_inc=-5, rev=100, ni=-10,
                eps=-1.0, shares=1e9, ocf=-5, de=1.0)
    data = {"LOSS": {"fin": loss, "price": 100, "sector": "X"}}
    res = run_screen(data)
    assert any(e["symbol"] == "LOSS" and "profitable" in e["reason"] for e in res["excluded"])
