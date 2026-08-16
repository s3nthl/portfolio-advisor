"""Options-performance analytics — closed-position realized P&L, by strategy/time."""
from __future__ import annotations

from analytics.options_perf import build_performance


def _txn(date, net, pid, under, kind, contracts, effect, typ="TRADE"):
    return {"date": date, "type": typ, "position_id": pid, "underlying": under,
            "kind": kind, "symbol": under, "contracts": contracts,
            "effect": effect, "net": net}


def test_closed_position_booked_on_close_date():
    txns = [
        _txn("2026-01-05", 900.0, 1, "NVDA", "PUT", -1, "OPENING"),   # sell CSP (Jan)
        _txn("2026-02-20", -100.0, 1, "NVDA", "PUT", 1, "CLOSING"),   # buy to close (Feb) -> closed, +800
        _txn("2026-02-10", 500.0, 2, "GLW", "PUT", -1, "OPENING"),    # still open (never closed)
    ]
    d = build_performance(txns, open_positions=[{"symbol": "GLW", "pl_open": -50.0}], as_of="2026-02-25")
    assert d["totals"]["realized"] == 800.0            # only the CLOSED position
    assert d["totals"]["closed_positions"] == 1
    assert d["totals"]["unrealized_open"] == -50.0
    monthly = {r["period"]: r for r in d["series"]["monthly"]}
    # the whole +800 lands in the CLOSE month (Feb), not smeared into Jan
    assert "2026-01" not in monthly
    assert monthly["2026-02"]["net"] == 800.0


def test_assignment_closes_a_short_put():
    # sell to open a put, then it's assigned (RECEIVE_AND_DELIVER offsets the contract) -> closed, premium kept
    txns = [
        _txn("2026-03-01", 300.0, 5, "AMD", "PUT", -1, "OPENING"),
        _txn("2026-03-15", 0.0, 5, "AMD", "PUT", 1, "CLOSING", typ="RECEIVE_AND_DELIVER"),
    ]
    d = build_performance(txns, [], as_of="2026-03-20")
    assert d["totals"]["realized"] == 300.0 and d["totals"]["closed_positions"] == 1


def test_strategy_split_separates_wheel_from_directional():
    txns = [
        # CSP winner
        _txn("2026-04-01", 400.0, 10, "AMZN", "PUT", -1, "OPENING"),
        _txn("2026-04-10", -50.0, 10, "AMZN", "PUT", 1, "CLOSING"),
        # long call (LEAP) loser — directional, must NOT be lumped with wheel
        _txn("2026-04-02", -2000.0, 11, "NVDA", "CALL", 1, "OPENING"),
        _txn("2026-04-20", 1500.0, 11, "NVDA", "CALL", -1, "CLOSING"),
    ]
    d = build_performance(txns, [], as_of="2026-04-25")
    strat = {s["strategy"]: s for s in d["by_strategy"]}
    assert strat["CSP"]["net"] == 350.0
    assert strat["Long call / LEAP"]["net"] == -500.0
    assert d["totals"]["realized"] == -150.0
    nvda = next(r for r in d["by_ticker"] if r["symbol"] == "NVDA")
    assert nvda["net"] == -500.0                        # NVDA shows its true (losing) option P&L
