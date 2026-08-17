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


WHEEL_STRATEGIES = ("CSP", "Covered call")


def _strategy(kinds: set, opens: list) -> str:
    """Classify from the opening leg's direction + option type."""
    short = (opens[0] < 0) if opens else True
    has_put, has_call = "PUT" in kinds, "CALL" in kinds
    if has_put and not has_call:
        return "CSP" if short else "Long put"
    if has_call and not has_put:
        return "Covered call" if short else "Long call / LEAP"
    return "Spread / mixed"


def _capital(strategy: str, strike, open_contracts: float, open_debit: float) -> float:
    """Capital at risk for the position — the base for ROC.
    Short wheel legs: strike x 100 x contracts (cash secured / shares covered).
    Long options: the premium paid (the debit)."""
    if strategy in WHEEL_STRATEGIES and strike:
        return round(abs(strike) * 100 * abs(open_contracts), 2)
    return round(abs(open_debit), 2)


def _new_cycle() -> dict:
    return {"net": 0.0, "contracts": 0.0, "dates": [], "kinds": set(), "opens": [],
            "underlying": None, "trades": 0, "open_strike": None,
            "open_contracts": 0.0, "open_debit": 0.0}


def _emit(c: dict, closed: bool, out: list) -> None:
    if not c["dates"]:
        return
    strat = _strategy(c["kinds"], c["opens"])
    out.append({
        "underlying": c["underlying"] or "?",
        "net": round(c["net"], 2),
        "closed": closed,
        "close_date": max(c["dates"]),
        "open_date": min(c["dates"]),
        "strategy": strat,
        "trades": c["trades"],
        "cap": _capital(strat, c["open_strike"], c["open_contracts"], c["open_debit"]),
        "days": max((max(c["dates"]) - min(c["dates"])).days, 1),
        "_strike": c["open_strike"], "_qty": c["open_contracts"],
    })


def _positions(txns: list[dict]) -> list[dict]:
    """Collapse trade legs into positions by walking each option CONTRACT (OSI
    symbol) through open→close CYCLES: accumulate legs until the contract count
    returns to 0, then bank a closed position and start a fresh cycle.

    Keyed on the contract symbol, NOT Schwab's position_id — recent trades often
    have no position_id yet (assigned after settlement), which would otherwise
    strand a close and drop its realized P&L. Assignment/expiry legs are included
    so the count zeroes out correctly, and each symbol is a single option type."""
    by_sym: dict = defaultdict(list)
    for t in txns:
        by_sym[t.get("symbol") or "?"].append(t)
    out: list = []
    for trades in by_sym.values():
        trades.sort(key=lambda t: (t.get("date", ""), 0 if t.get("effect") == "OPENING" else 1))
        cyc = None
        for t in trades:
            if cyc is None:
                cyc = _new_cycle()
            cyc["net"] += t.get("net") or 0.0
            cyc["contracts"] += t.get("contracts") or 0.0
            d = _parse(t.get("date", ""))
            if d:
                cyc["dates"].append(d)
            if t.get("kind"):
                cyc["kinds"].add(t["kind"])
            if t.get("effect") == "OPENING":
                cyc["opens"].append(t.get("contracts") or 0.0)
                cyc["open_contracts"] += abs(t.get("contracts") or 0.0)
                cyc["open_debit"] += t.get("net") or 0.0
                if cyc["open_strike"] is None and t.get("strike") is not None:
                    cyc["open_strike"] = t.get("strike")
            if t.get("type") == "TRADE":
                cyc["trades"] += 1
            cyc["underlying"] = cyc["underlying"] or t.get("underlying")
            if abs(cyc["contracts"]) < 1e-6:         # cycle complete → closed position
                _emit(cyc, True, out); cyc = None
        if cyc is not None:                          # leftover legs → still open
            _emit(cyc, False, out)
    return _pair_spreads(out)


def _pair_spreads(positions: list[dict]) -> list[dict]:
    """Merge same-underlying, same-open-day short+long legs of the SAME option type
    into one spread position. A vertical's short and long legs otherwise get split
    across the "Covered call"/"Long call" (or "CSP"/"Long put") buckets and wildly
    distort both — e.g. a SPY 720/725 call spread shows as −$21k covered call and
    +$18k long call instead of its true −$3k. Genuine single-leg wheel positions
    (a short call/put with no same-day paired long) are untouched."""
    SHORT_OF = {"Covered call": "CALL", "CSP": "PUT"}
    LONG_OF = {"Long call / LEAP": "CALL", "Long put": "PUT"}
    buckets: dict = defaultdict(list)
    passthrough: list = []
    for p in positions:
        typ = SHORT_OF.get(p["strategy"]) or LONG_OF.get(p["strategy"])
        if typ:
            buckets[(p["underlying"], p["open_date"], typ)].append(p)
        else:
            passthrough.append(p)
    out = list(passthrough)
    for (under, _open, typ), grp in buckets.items():
        shorts = [p for p in grp if p["strategy"] in SHORT_OF]
        longs = [p for p in grp if p["strategy"] in LONG_OF]
        if shorts and longs:                       # a vertical spread — merge the legs
            strikes = [p["_strike"] for p in grp if p["_strike"]]
            qty = max((p["_qty"] for p in grp), default=1) or 1
            width = (max(strikes) - min(strikes)) if len(strikes) >= 2 else 0
            cap = round(width * 100 * qty, 2) if width else round(max(p["cap"] for p in grp), 2)
            out.append({
                "underlying": under, "net": round(sum(p["net"] for p in grp), 2),
                "closed": all(p["closed"] for p in grp),
                "close_date": max(p["close_date"] for p in grp),
                "open_date": min(p["open_date"] for p in grp),
                "strategy": f"{typ.capitalize()} spread",
                "trades": sum(p["trades"] for p in grp),
                "cap": cap,
                "days": max((max(p["close_date"] for p in grp) - min(p["open_date"] for p in grp)).days, 1),
            })
        else:
            out.extend(grp)                         # single-leg (real wheel / directional)
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

    # Return on capital, annualized: realized ÷ capital-YEARS at risk (each
    # position's capital weighted by how long it was held). Because capital is
    # reused as positions roll, we report the AVERAGE capital at risk over the
    # window (capital-years ÷ window length), not the meaningless running sum.
    cap_years = sum(p["cap"] * p["days"] / 365.0 for p in closed)
    if closed:
        span_days = max((dates[-1] - min(p["open_date"] for p in closed)).days, 1)
    else:
        span_days = 1
    avg_capital = round(cap_years / (span_days / 365.0)) if cap_years else 0
    roc_annual = round(total / cap_years * 100, 1) if cap_years else None
    roc_pct = round(total / avg_capital * 100, 1) if avg_capital else None  # over the window

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
                   "avg_capital": avg_capital,
                   "roc_pct": roc_pct, "roc_annual_pct": roc_annual,
                   "open_count": len(open_positions), "unrealized_open": unreal},
        "by_strategy": by_strategy,
        "series": {g: _series(closed, g) for g in ("daily", "weekly", "monthly")},
        "by_ticker": by_ticker,
        # per-closed-position detail (booked on close date) — drives the calendar
        # heatmap and the click-through "where did this day's number come from".
        "closed_detail": [{"d": p["close_date"].isoformat(), "u": p["underlying"],
                           "s": p["strategy"], "net": p["net"],
                           "opened": p["open_date"].isoformat(), "trades": p["trades"],
                           "cap": p["cap"], "days": p["days"]}
                          for p in sorted(closed, key=lambda p: p["close_date"])],
        "open_positions": sorted(open_positions, key=lambda p: (p.get("pl_open") or 0)),
    }
