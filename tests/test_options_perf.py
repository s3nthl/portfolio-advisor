"""Options-performance analytics — pure-function regression tests."""
from __future__ import annotations

from analytics.options_perf import build_performance


def _txn(date, net, pid, under, kind, contracts, typ="TRADE"):
    return {"date": date, "type": typ, "position_id": pid, "underlying": under,
            "kind": kind, "symbol": under, "contracts": contracts,
            "effect": "OPENING" if contracts else "CLOSING", "net": net}


def test_realized_cashflow_and_buckets():
    txns = [
        _txn("2026-01-05", 900.0, 1, "NVDA", "PUT", -1),   # sell to open (premium in)
        _txn("2026-01-20", -100.0, 1, "NVDA", "PUT", 1),   # buy to close (position 1 now closed, +800 net)
        _txn("2026-02-10", 500.0, 2, "GLW", "CALL", -2),   # still open
        _txn("2026-02-11", 0.0, 3, "GLW", "PUT", -1, "RECEIVE_AND_DELIVER"),  # assignment: ignored for P&L
    ]
    d = build_performance(txns, open_positions=[{"pl_open": -50.0}], as_of="2026-02-12")
    assert d["status"] == "ok"
    # realized cash basis = 900 - 100 + 500 = 1300 (the $0 assignment/RECEIVE excluded from trades)
    assert d["totals"]["net"] == 1300.0
    assert d["totals"]["premium_in"] == 1400.0 and d["totals"]["paid_out"] == -100.0
    assert d["totals"]["trades"] == 3          # only TRADE-type
    assert d["totals"]["unrealized_open"] == -50.0

    monthly = {r["period"]: r for r in d["series"]["monthly"]}
    assert monthly["2026-01"]["net"] == 800.0
    assert monthly["2026-02"]["net"] == 500.0
    assert monthly["2026-02"]["cumulative"] == 1300.0   # running total


def test_open_vs_closed_and_win_rate():
    txns = [
        _txn("2026-03-01", 300.0, 10, "AMZN", "PUT", -1),   # closed winner
        _txn("2026-03-08", -50.0, 10, "AMZN", "PUT", 1),
        _txn("2026-03-02", 200.0, 11, "AMZN", "PUT", -1),   # still open (net contracts != 0)
    ]
    d = build_performance(txns, open_positions=[], as_of="2026-03-10")
    amzn = next(r for r in d["by_ticker"] if r["symbol"] == "AMZN")
    assert amzn["open"] is True                 # position 11 unclosed
    assert amzn["closed"] == 1 and amzn["wins"] == 1 and amzn["win_rate"] == 100
    assert amzn["net"] == 450.0
