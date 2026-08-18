"""Stock (equity/ETF) realized P&L from CLOSED share lots — FIFO matched.

Pure functions, no I/O. Answers "what did I actually realize on stock — closed
round-trips — by day/week/month?", kept strictly SEPARATE from options P&L
(never blend, per the wheel methodology): a CSP-assigned share's cost basis is
its strike, and the option premium stays in the options universe. Realized stock
P&L on a sale = sale proceeds − FIFO cost of the shares sold, booked on the SALE
date. Output shape matches options (closed_detail rows), so the same dashboard
(calendar, by-ticker, series, ROC) renders both.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime

from analytics.options_perf import aggregate_from_detail


def _parse(s: str):
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _closed_lots(txns: list[dict]) -> tuple[list[dict], dict]:
    """FIFO-match equity acquisitions against disposals per symbol.

    Each disposal (sell / called-away) banks one realized row for the portion of
    shares whose cost basis is KNOWN (an in-window acquisition lot). Shares sold
    that have no matching lot were bought before the transaction history starts —
    their basis is unavailable, so rather than fabricate a $0 gain (which would
    dilute win-rate and understate real P&L) they are EXCLUDED from realized and
    tallied into `unpriced` for an honest caveat on the view.

    Returns (detail_rows, unpriced_summary)."""
    by_sym: dict = defaultdict(list)
    for t in txns:
        by_sym[t.get("symbol") or "?"].append(t)
    detail: list[dict] = []
    up_sales = 0
    up_shares = 0.0
    up_proceeds = 0.0
    up_syms: set = set()
    for sym, ts in by_sym.items():
        # same-day: acquisitions before disposals so a buy+sell that day matches
        ts.sort(key=lambda t: (t.get("date", ""), 0 if (t.get("shares") or 0) > 0 else 1))
        lots: deque = deque()                       # [shares_left, cost_per_share, acquire_date]
        for t in ts:
            sh = float(t.get("shares") or 0.0)
            if abs(sh) < 1e-9:
                continue
            d = _parse(t.get("date", ""))
            if d is None:
                continue
            net = float(t.get("net") or 0.0)
            if sh > 0:                              # acquisition -> new lot
                cps = abs(net) / sh if net else float(t.get("price") or 0.0)
                lots.append([sh, cps, d])
                continue
            qty = -sh                               # disposal -> consume FIFO lots
            proceeds_ps = abs(net) / qty if net else float(t.get("price") or 0.0)
            realized, cost, matched, open_date, remaining = 0.0, 0.0, 0.0, d, qty
            while remaining > 1e-9 and lots:
                lot = lots[0]
                take = min(remaining, lot[0])
                realized += (proceeds_ps - lot[1]) * take
                cost += lot[1] * take
                matched += take
                open_date = min(open_date, lot[2])
                lot[0] -= take
                remaining -= take
                if lot[0] <= 1e-9:
                    lots.popleft()
            if remaining > 1e-9:                    # basis unknown (bought before history)
                up_shares += remaining
                up_proceeds += proceeds_ps * remaining
                up_syms.add(sym)
                if matched <= 1e-9:
                    up_sales += 1
            if matched > 1e-9:                      # book only the priceable portion
                detail.append({
                    "d": d.isoformat(), "u": sym, "s": "Stock",
                    "net": round(realized, 2), "opened": open_date.isoformat(),
                    "trades": 1, "cap": round(cost, 2),
                    "days": max((d - open_date).days, 1),
                    "partial": remaining > 1e-9,
                })
    detail.sort(key=lambda r: r["d"])
    unpriced = {"sales": up_sales, "shares": round(up_shares),
                "proceeds": round(up_proceeds, 2), "symbols": sorted(up_syms)}
    return detail, unpriced


def build_stock_performance(txns: list[dict], open_positions: list[dict],
                            as_of: str | None = None) -> dict:
    """Realized stock/ETF P&L payload (same shape as build_performance), plus an
    `unpriced` block for sales whose shares were bought before the history window."""
    detail, unpriced = _closed_lots(txns)
    payload = aggregate_from_detail(detail, open_positions, as_of=as_of)
    payload["unpriced"] = unpriced
    return payload
