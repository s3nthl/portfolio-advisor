"""Options performance — realized P&L from trade history, bucketed by time.

Pure functions, no I/O (the API layer fetches the transactions + open book and
passes them in). Methodology, honoring the wheel P&L rules:

- Realized options P&L is on a CASH basis: sum of each option trade's `net`
  (after-fee cash flow). Sell-to-open premium is +, buy-to-close is −,
  assignments/expirations are 0 (premium already booked at open). The running
  sum over any window is the true realized cash P&L from options.
- Options are their OWN P&L universe — assignment stock legs live on the stock
  side and never enter here.
- A position (by position_id) is CLOSED when its net contract count returns to
  0; otherwise it's still OPEN and its collected premium is provisional.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())          # Monday of that week


def _bucket_key(d: date, gran: str) -> str:
    if gran == "daily":
        return d.isoformat()
    if gran == "weekly":
        return _week_start(d).isoformat()
    return f"{d.year:04d}-{d.month:02d}"             # monthly


def _parse(s: str) -> date | None:
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _series(trades: list[dict], gran: str) -> list[dict]:
    """Per-period realized P&L + trade count + cumulative, oldest→newest."""
    agg: dict[str, dict] = {}
    for t in trades:
        d = _parse(t.get("date", ""))
        if d is None:
            continue
        k = _bucket_key(d, gran)
        b = agg.setdefault(k, {"period": k, "net": 0.0, "trades": 0,
                               "premium": 0.0, "paid": 0.0})
        net = t["net"]
        b["net"] += net
        b["trades"] += 1
        if net >= 0:
            b["premium"] += net
        else:
            b["paid"] += net
    rows = [ {**v, "net": round(v["net"], 2), "premium": round(v["premium"], 2),
              "paid": round(v["paid"], 2)} for v in agg.values() ]
    rows.sort(key=lambda r: r["period"])
    run = 0.0
    for r in rows:
        run += r["net"]
        r["cumulative"] = round(run, 2)
    return rows


def _by_ticker(trades: list[dict], open_ids: set) -> list[dict]:
    agg: dict[str, dict] = {}
    for t in trades:
        u = t.get("underlying") or "?"
        b = agg.setdefault(u, {"symbol": u, "net": 0.0, "trades": 0,
                               "puts": 0, "calls": 0, "wins": 0, "closed": 0,
                               "open": False})
        b["net"] += t["net"]
        b["trades"] += 1
        if t.get("kind") == "PUT":
            b["puts"] += 1
        elif t.get("kind") == "CALL":
            b["calls"] += 1
        if t.get("position_id") in open_ids:
            b["open"] = True
    # win rate is over CLOSED positions (a position_id whose net cash is known-final)
    pos: dict = {}
    for t in trades:
        pid = t.get("position_id")
        if pid is None:
            continue
        p = pos.setdefault(pid, {"u": t.get("underlying"), "net": 0.0, "open": False})
        p["net"] += t["net"]
        if pid in open_ids:
            p["open"] = True
    for p in pos.values():
        if not p["open"] and p["u"] in agg:
            agg[p["u"]]["closed"] += 1
            if p["net"] > 0:
                agg[p["u"]]["wins"] += 1
    rows = []
    for b in agg.values():
        b["net"] = round(b["net"], 2)
        b["win_rate"] = round(b["wins"] / b["closed"] * 100) if b["closed"] else None
        rows.append(b)
    rows.sort(key=lambda r: r["net"], reverse=True)
    return rows


def build_performance(txns: list[dict], open_positions: list[dict],
                      as_of: str | None = None) -> dict:
    """Assemble the options-performance payload from raw option txns + open book.

    `open_positions`: [{symbol, underlying, kind, contracts, dte, pl_open, notional}].
    """
    trades = [t for t in txns if t.get("type") == "TRADE" and t.get("net") is not None]

    # which position_ids are still open (net contracts != 0)
    net_contracts: dict = {}
    for t in txns:
        pid = t.get("position_id")
        if pid is None:
            continue
        net_contracts[pid] = net_contracts.get(pid, 0.0) + (t.get("contracts") or 0.0)
    open_ids = {pid for pid, n in net_contracts.items() if abs(n) > 1e-6}

    dates = sorted(d for d in (_parse(t.get("date", "")) for t in trades) if d)
    total_net = round(sum(t["net"] for t in trades), 2)
    premium = round(sum(t["net"] for t in trades if t["net"] >= 0), 2)
    paid = round(sum(t["net"] for t in trades if t["net"] < 0), 2)
    unreal = round(sum(p.get("pl_open") or 0 for p in open_positions), 2)

    return {
        "status": "ok",
        "as_of": as_of,
        "from": dates[0].isoformat() if dates else None,
        "to": dates[-1].isoformat() if dates else None,
        "totals": {"net": total_net, "premium_in": premium, "paid_out": paid,
                   "trades": len(trades), "unrealized_open": unreal,
                   "open_count": len(open_positions)},
        "series": {g: _series(trades, g) for g in ("daily", "weekly", "monthly")},
        "by_ticker": _by_ticker(trades, open_ids),
        "open_positions": sorted(open_positions,
                                 key=lambda p: (p.get("pl_open") or 0)),
    }
