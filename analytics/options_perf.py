"""Options performance — realized P&L from CLOSED positions, bucketed by time.

Pure functions, no I/O. The question this answers: "what did I actually make in
options — closed positions — by day/week/month?"

Method (honors the wheel P&L rules):
- Trades are grouped into POSITIONS by Schwab's position_id. A position's realized
  P&L is the sum of every leg's after-fee net cash (sell-to-open premium +,
  buy-to-close −; assignments/expirations are 0, already booked). A position is
  CLOSED when its net contract count returns to 0 (a buy-back, expiry, OR an
  assignment all close it); only closed positions count as realized.
- Each closed position is booked in the period of its CLOSE date — so a premium
  sold in one month and bought back the next lands entirely in the close month,
  not smeared across both.
- Positions are split by STRATEGY and never blended: short put = CSP, short call =
  covered call, long call = LEAP/long call, long put = long put. This separates
  wheel income from directional long-option (stock-like) bets.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

STRATEGIES = ["CSP", "Covered call", "Long call / LEAP", "Long put", "Spread / mixed"]


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


def _strategy(kinds: set, opens: list) -> str:
    """Classify from the opening leg's direction + option type."""
    short = (opens[0] < 0) if opens else True
    has_put, has_call = "PUT" in kinds, "CALL" in kinds
    if has_put and not has_call:
        return "CSP" if short else "Long put"
    if has_call and not has_put:
        return "Covered call" if short else "Long call / LEAP"
    return "Spread / mixed"


def _positions(txns: list[dict]) -> list[dict]:
    """Collapse trade legs into positions (incl. assignment/expiry legs so the
    contract count — hence closed-detection and close date — is correct)."""
    groups: dict = defaultdict(lambda: {"net": 0.0, "contracts": 0.0, "dates": [],
                                        "kinds": set(), "opens": [], "underlying": None,
                                        "trades": 0})
    solo = 0
    for t in txns:
        pid = t.get("position_id")
        if pid is None:                              # ~1% of legs; give each its own bucket
            pid = f"solo-{solo}"; solo += 1
        g = groups[pid]
        g["net"] += t.get("net") or 0.0
        g["contracts"] += t.get("contracts") or 0.0
        d = _parse(t.get("date", ""))
        if d:
            g["dates"].append(d)
        if t.get("kind"):
            g["kinds"].add(t["kind"])
        if t.get("effect") == "OPENING":
            g["opens"].append(t.get("contracts") or 0.0)
        if t.get("type") == "TRADE":
            g["trades"] += 1
        g["underlying"] = g["underlying"] or t.get("underlying")
    out = []
    for g in groups.values():
        if not g["dates"]:
            continue
        out.append({
            "underlying": g["underlying"] or "?",
            "net": round(g["net"], 2),
            "closed": abs(g["contracts"]) < 1e-6,
            "close_date": max(g["dates"]),
            "open_date": min(g["dates"]),
            "strategy": _strategy(g["kinds"], g["opens"]),
            "trades": g["trades"],
        })
    return out


def _series(closed: list[dict], gran: str) -> list[dict]:
    """Realized P&L per period, booked on each position's CLOSE date."""
    agg: dict = defaultdict(lambda: {"net": 0.0, "positions": 0, "wins": 0})
    for p in closed:
        k = _bucket_key(p["close_date"], gran)
        b = agg[k]
        b["net"] += p["net"]; b["positions"] += 1
        if p["net"] > 0:
            b["wins"] += 1
    rows = [{"period": k, "net": round(v["net"], 2), "positions": v["positions"],
             "wins": v["wins"]} for k, v in agg.items()]
    rows.sort(key=lambda r: r["period"])
    run = 0.0
    for r in rows:
        run += r["net"]; r["cumulative"] = round(run, 2)
    return rows


def build_performance(txns: list[dict], open_positions: list[dict],
                      as_of: str | None = None) -> dict:
    positions = _positions(txns)
    closed = [p for p in positions if p["closed"]]

    dates = sorted(p["close_date"] for p in closed)
    total = round(sum(p["net"] for p in closed), 2)
    wins = sum(1 for p in closed if p["net"] > 0)
    unreal = round(sum(x.get("pl_open") or 0 for x in open_positions), 2)

    # by strategy — wheel income vs directional, never blended
    strat: dict = defaultdict(lambda: {"net": 0.0, "positions": 0, "wins": 0})
    for p in closed:
        s = strat[p["strategy"]]
        s["net"] += p["net"]; s["positions"] += 1
        if p["net"] > 0:
            s["wins"] += 1
    by_strategy = [{"strategy": k, "net": round(v["net"], 2), "positions": v["positions"],
                    "win_rate": round(v["wins"] / v["positions"] * 100) if v["positions"] else None}
                   for k, v in strat.items()]
    by_strategy.sort(key=lambda r: r["net"], reverse=True)

    # by ticker (closed only)
    tick: dict = defaultdict(lambda: {"net": 0.0, "positions": 0, "wins": 0, "open": False})
    for p in closed:
        u = tick[p["underlying"]]
        u["net"] += p["net"]; u["positions"] += 1
        if p["net"] > 0:
            u["wins"] += 1
    open_underlyings = {op.get("symbol") for op in open_positions}
    for u in open_underlyings:
        tick[u]["open"] = True
    by_ticker = [{"symbol": k, "net": round(v["net"], 2), "positions": v["positions"],
                  "win_rate": round(v["wins"] / v["positions"] * 100) if v["positions"] else None,
                  "open": v["open"]}
                 for k, v in tick.items() if v["positions"] or v["open"]]
    by_ticker.sort(key=lambda r: r["net"], reverse=True)

    return {
        "status": "ok",
        "as_of": as_of,
        "from": dates[0].isoformat() if dates else None,
        "to": dates[-1].isoformat() if dates else None,
        "totals": {"realized": total, "closed_positions": len(closed),
                   "win_rate": round(wins / len(closed) * 100) if closed else None,
                   "avg_per_position": round(total / len(closed), 2) if closed else 0,
                   "open_count": len(open_positions), "unrealized_open": unreal},
        "by_strategy": by_strategy,
        "series": {g: _series(closed, g) for g in ("daily", "weekly", "monthly")},
        "by_ticker": by_ticker,
        "open_positions": sorted(open_positions, key=lambda p: (p.get("pl_open") or 0)),
    }
