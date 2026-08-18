"""Stock-performance analytics — FIFO realized P&L on closed equity round-trips."""
from __future__ import annotations

from analytics.stock_perf import build_stock_performance, _closed_lots


def _t(date, shares, net, sym="XYZ", price=0.0, typ="TRADE"):
    return {"date": date, "type": typ, "symbol": sym,
            "shares": shares, "price": price, "net": net}


def test_fifo_realized_on_a_simple_round_trip():
    # buy 100 @ $10 (−$1000), sell 100 @ $15 (+$1500) -> +$500 realized, booked on the sell
    txns = [_t("2026-01-05", 100, -1000.0), _t("2026-03-06", -100, 1500.0)]
    d = build_stock_performance(txns, [], as_of="2026-03-10")
    t = d["totals"]
    assert t["realized"] == 500.0 and t["closed_positions"] == 1 and t["win_rate"] == 100
    cd = d["closed_detail"][0]
    assert cd["d"] == "2026-03-06" and cd["opened"] == "2026-01-05" and cd["cap"] == 1000.0
    assert d["unpriced"]["sales"] == 0
    # the whole +500 lands in the SELL month
    monthly = {r["period"]: r for r in d["series"]["monthly"]}
    assert monthly["2026-03"]["net"] == 500.0 and "2026-01" not in monthly


def test_fifo_matches_oldest_lot_first():
    # two buys then one sell of everything: 50@10 then 50@20, sell 100@30
    # realized = (30-10)*50 + (30-20)*50 = 1000 + 500 = 1500
    txns = [_t("2026-01-01", 50, -500.0), _t("2026-01-15", 50, -1000.0),
            _t("2026-02-01", -100, 3000.0)]
    d = build_stock_performance(txns, [], as_of="2026-02-05")
    assert d["totals"]["realized"] == 1500.0
    assert d["closed_detail"][0]["cap"] == 1500.0          # cost basis of the shares sold
    assert d["closed_detail"][0]["opened"] == "2026-01-01"  # oldest consumed lot


def test_shares_bought_before_history_are_excluded_not_fabricated():
    # sell 20 with NO prior buy in the window -> basis unknown: excluded, flagged, $0 realized
    txns = [_t("2026-01-03", -20, 7727.0, sym="TSLA", price=386.35)]
    d = build_stock_performance(txns, [], as_of="2026-01-10")
    assert d["totals"]["closed_positions"] == 0            # no fabricated realized row
    assert d["totals"]["realized"] == 0.0
    u = d["unpriced"]
    assert u["sales"] == 1 and u["shares"] == 20 and "TSLA" in u["symbols"]


def test_partial_match_books_priceable_part_and_flags_the_rest():
    # own 60 in-window (buy 60 @ $10), sell 100 @ $12 -> price 60, 40 unpriced
    txns = [_t("2026-01-01", 60, -600.0), _t("2026-02-01", -100, 1200.0)]
    detail, unpriced = _closed_lots(txns)
    assert len(detail) == 1 and detail[0]["partial"] is True
    assert detail[0]["net"] == round((12 - 10) * 60, 2)   # +120 on the matched 60
    assert unpriced["shares"] == 40 and unpriced["sales"] == 0  # partial, not a full miss
