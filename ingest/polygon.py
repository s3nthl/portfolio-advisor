"""Polygon.io (Massive) — deep financial statements (15+ yrs), cached daily.

Paid key. Polygon's standardized statements go far deeper than FMP's free tier
(16 fiscal years vs 5) but carry fewer line items (no EBITDA/Capex/FCF/cash),
so this covers the core metrics + the valuation basis; FMP supplies the extras.

Polygon occasionally returns a spurious 401 under burst — we retry with backoff.
Never raises: returns empty metrics on failure.
"""
from __future__ import annotations

import json
import time
from datetime import date

import config

_BASE = "https://api.polygon.io"
_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _yr_lbl(fiscal_year, end_date):
    y = str(fiscal_year) if fiscal_year else (end_date or "")[:4]
    return f"'{y[2:]}" if len(y) >= 2 else y


def _row_lbl(row):
    """Annual -> \"'26\"; quarterly -> \"Q2'26\"."""
    fp = row.get("fiscal_period")
    y = str(row.get("fiscal_year") or (row.get("end_date") or "")[:4])
    if fp and fp.startswith("Q") and len(y) >= 2:
        return f"{fp}'{y[2:]}"
    return f"'{y[2:]}" if len(y) >= 2 else y


def _month_lbl(ym):
    """'2026-07' -> \"Jul'26\"."""
    try:
        y, m = ym.split("-")
        return f"{_MONTHS[int(m)]}'{y[2:]}"
    except Exception:
        return ym


def _v(stmt: dict, key: str):
    x = stmt.get(key)
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x) if x is not None else None
    except Exception:
        return None


def _get(url: str, retries: int = 6):
    """GET with retry/backoff — Polygon throws spurious 401/429s under burst."""
    import httpx
    for i in range(retries):
        try:
            r = httpx.get(url, timeout=25.0)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (401, 429, 500, 502, 503) and i < retries - 1:
                time.sleep(1.0 + 0.8 * i)  # escalating backoff
                continue
            return {}
        except Exception:
            time.sleep(1.0)
    return {}


def _splits(sym: str, key: str) -> list[tuple[str, float]]:
    """[(ex_date, ratio)] where ratio = split_to/split_from (e.g. 20 for a 20:1)."""
    j = _get(f"{_BASE}/v3/reference/splits?ticker={sym}&limit=100&apiKey={key}")
    out = []
    for s in (j.get("results") or []):
        ed, sf, st = s.get("execution_date"), s.get("split_from"), s.get("split_to")
        if ed and sf and st:
            try:
                out.append((ed, float(st) / float(sf)))
            except Exception:
                pass
    return out


def _split_factor(date_str: str, splits: list[tuple[str, float]]) -> float:
    """Cumulative split ratio for splits that took effect AFTER `date_str`.

    Multiplying share counts (and dividing EPS) by this converts an as-reported
    historical figure into today's split-adjusted terms — so it lines up with the
    already-split-adjusted price history (essential for correct P/E & P/S).
    """
    f = 1.0
    for ed, ratio in (splits or []):
        if ed > date_str:
            f *= ratio
    return f


def _build(results: list[dict], splits: list[tuple[str, float]] | None = None) -> tuple[dict, list[dict]]:
    """results newest->oldest -> (metrics{name:{...bars}}, basis[]).

    Per-share figures (EPS) and share counts are split-adjusted to today's basis
    so they match the split-adjusted price; dollar totals are split-invariant.
    """
    rows = sorted(results, key=lambda r: r.get("end_date", ""))  # oldest->newest
    factors = [_split_factor(r.get("end_date", ""), splits) for r in rows]
    inc = [(_row_lbl(r),
            r.get("financials", {}).get("income_statement", {}),
            r.get("financials", {}).get("cash_flow_statement", {}),
            r.get("financials", {}).get("balance_sheet", {})) for r in rows]

    def series(fn):
        return [{"label": lbl, "v": fn(i, c, b)} for (lbl, i, c, b) in inc]

    def eps_series():   # split-adjusted diluted EPS (÷ post-date split factor)
        out = []
        for (lbl, i, c, b), f in zip(inc, factors):
            v = _v(i, "diluted_earnings_per_share")
            out.append({"label": lbl, "v": (v / f) if (v is not None and f) else v})
        return out

    def margin(i, c, b):
        rev, gp = _v(i, "revenues"), _v(i, "gross_profit")
        return (gp / rev * 100) if (rev and gp is not None) else None

    def net_margin(i, c, b):
        rev, ni = _v(i, "revenues"), _v(i, "net_income_loss")
        return (ni / rev * 100) if (rev and ni is not None) else None

    def roe(i, c, b):  # return on equity — profit per dollar of shareholder capital
        eq, ni = _v(b, "equity"), _v(i, "net_income_loss")
        return (ni / eq * 100) if (eq and eq > 0 and ni is not None) else None

    def debt_equity(i, c, b):  # leverage — long-term debt vs owners' capital
        eq, d = _v(b, "equity"), _v(b, "long_term_debt")
        return round(d / eq, 2) if (eq and eq > 0 and d is not None) else None

    # --- metrics derived from the SAME Polygon statements (no extra source) -------
    def op_margin(i, c, b):  # operating income as % of revenue — core-business efficiency
        rev, oi = _v(i, "revenues"), _v(i, "operating_income_loss")
        return (oi / rev * 100) if (rev and oi is not None) else None

    def roa(i, c, b):  # return on assets — profit per dollar of everything owned
        a, ni = _v(b, "assets"), _v(i, "net_income_loss")
        return (ni / a * 100) if (a and a > 0 and ni is not None) else None

    def tax_rate(i, c, b):  # effective tax rate = tax / pretax income
        pre = _v(i, "income_loss_from_continuing_operations_before_tax")
        tax = _v(i, "income_tax_expense_benefit")
        return (tax / pre * 100) if (pre and pre > 0 and tax is not None) else None

    def rnd_intensity(i, c, b):  # R&D as % of revenue — how much they reinvest in product
        rev, rnd = _v(i, "revenues"), _v(i, "research_and_development")
        return (rnd / rev * 100) if (rev and rnd is not None) else None

    def asset_turnover(i, c, b):  # revenue per dollar of assets — capital efficiency
        rev, a = _v(i, "revenues"), _v(b, "assets")
        return round(rev / a, 2) if (rev and a and a > 0) else None

    def current_ratio(i, c, b):  # short-term liquidity — current assets vs current liabilities
        ca, cl = _v(b, "current_assets"), _v(b, "current_liabilities")
        return round(ca / cl, 2) if (ca is not None and cl and cl > 0) else None

    def working_capital(i, c, b):  # current assets minus current liabilities (cash cushion)
        ca, cl = _v(b, "current_assets"), _v(b, "current_liabilities")
        return (ca - cl) if (ca is not None and cl is not None) else None

    def _shares(i):
        # Polygon builds the fiscal-Q4 quarterly row as (annual − 9-month), which turns
        # flow items like the diluted-share average NEGATIVE/garbage. Reject non-positive
        # counts so Q4 shows a gap instead of a nonsense value.
        shs = _v(i, "diluted_average_shares")
        if shs is None or shs <= 0:
            shs = _v(i, "basic_average_shares")
        return shs if (shs and shs > 0) else None

    def bvps_series():  # book value per share — equity ÷ split-adjusted diluted shares
        out = []
        for (lbl, i, c, b), f in zip(inc, factors):
            eq, shs = _v(b, "equity"), _shares(i)
            out.append({"label": lbl, "v": (eq / (shs * f)) if (eq is not None and shs and f) else None})
        return out

    def shares_series():  # diluted share count in today's split terms — dilution vs buybacks
        out = []
        for (lbl, i, c, b), f in zip(inc, factors):
            shs = _shares(i)
            out.append({"label": lbl, "v": (shs * f) if (shs is not None and f) else None})
        return out

    metrics = {
        "Revenue":            {"unit": "$", "good": True,  "kind": "bar", "data": series(lambda i, c, b: _v(i, "revenues"))},
        "Gross Profit":       {"unit": "$", "good": True,  "kind": "bar", "data": series(lambda i, c, b: _v(i, "gross_profit"))},
        "Gross Margin":       {"unit": "%", "good": True,  "kind": "bar", "data": series(margin)},
        "Operating Income":   {"unit": "$", "good": True,  "kind": "bar", "data": series(lambda i, c, b: _v(i, "operating_income_loss"))},
        "Operating Margin":   {"unit": "%", "good": True,  "kind": "bar", "data": series(op_margin)},
        "Net Income":         {"unit": "$", "good": True,  "kind": "bar", "data": series(lambda i, c, b: _v(i, "net_income_loss"))},
        "Net Margin":         {"unit": "%", "good": True,  "kind": "bar", "data": series(net_margin)},
        "Effective Tax Rate": {"unit": "%", "good": False, "kind": "bar", "data": series(tax_rate)},
        "R&D Intensity":      {"unit": "%", "good": True,  "kind": "bar", "data": series(rnd_intensity)},
        "Return on Equity":   {"unit": "%", "good": True,  "kind": "bar", "data": series(roe)},
        "Return on Assets":   {"unit": "%", "good": True,  "kind": "bar", "data": series(roa)},
        "EPS (diluted)":      {"unit": "eps", "good": True, "kind": "bar", "data": eps_series()},
        "Shares Outstanding": {"unit": "sh", "good": False, "kind": "bar", "data": shares_series()},
        "Cash from Operations": {"unit": "$", "good": True, "kind": "bar", "data": series(lambda i, c, b: _v(c, "net_cash_flow_from_operating_activities"))},
        "Investing Cash Flow": {"unit": "$", "good": False, "kind": "bar", "data": series(lambda i, c, b: _v(c, "net_cash_flow_from_investing_activities"))},
        "Financing Cash Flow": {"unit": "$", "good": False, "kind": "bar", "data": series(lambda i, c, b: _v(c, "net_cash_flow_from_financing_activities"))},
        "Total Assets":       {"unit": "$", "good": True, "kind": "bar", "data": series(lambda i, c, b: _v(b, "assets"))},
        "Asset Turnover":     {"unit": "x", "good": True, "kind": "bar", "data": series(asset_turnover)},
        "Current Ratio":      {"unit": "x", "good": True, "kind": "bar", "data": series(current_ratio)},
        "Working Capital":    {"unit": "$", "good": True, "kind": "bar", "data": series(working_capital)},
        "Shareholder Equity": {"unit": "$", "good": True, "kind": "bar", "data": series(lambda i, c, b: _v(b, "equity"))},
        "Book Value / Share": {"unit": "eps", "good": True, "kind": "bar", "data": bvps_series()},
        "Long-term Debt":     {"unit": "$", "good": False, "kind": "bar", "data": series(lambda i, c, b: _v(b, "long_term_debt"))},
        "Debt / Equity":      {"unit": "x", "good": False, "kind": "bar", "data": series(debt_equity)},
    }
    # basis for P/E & P/S — EPS and share count adjusted to today's split basis so
    # they line up with the split-adjusted price (revenue is split-invariant).
    basis = []
    for r, f in zip(rows, factors):
        inc_s = r.get("financials", {}).get("income_statement", {})
        eps, shs = _v(inc_s, "diluted_earnings_per_share"), _shares(inc_s)
        basis.append({"date": r.get("end_date"),
                      "eps": (eps / f) if (eps is not None and f) else eps,
                      "revenue": _v(inc_s, "revenues"),
                      "shares": (shs * f) if (shs is not None and f) else shs})
    return metrics, basis


def financials(symbol: str, timeframe: str = "annual", force: bool = False) -> dict:
    """Deep statement bars + valuation basis for `symbol`, cached daily."""
    tf = "annual" if str(timeframe).lower().startswith(("a", "y")) else "quarterly"
    sym = symbol.upper()
    ck = f"{sym}:{tf}"
    today = date.today().isoformat()
    try:
        cache = json.loads(config.POLYGON_FUND_CACHE_PATH.read_text())
    except Exception:
        cache = {}
    if not force and cache.get(ck, {}).get("as_of") == today:
        return cache[ck]["data"]

    key = config.POLYGON_API_KEY
    data = {"symbol": sym, "metrics": {}, "basis": [], "count": 0}
    if key:
        limit = config.POLYGON_FUND_LIMIT if tf == "annual" else 40  # ~10yr of quarters
        j = _get(f"{_BASE}/vX/reference/financials?ticker={sym}&timeframe={tf}"
                 f"&order=desc&limit={limit}&apiKey={key}")
        results = j.get("results") or []
        if results:
            splits = _splits(sym, key)   # split-adjust EPS/shares to match adjusted prices
            metrics, basis = _build(results, splits)
            data = {"symbol": sym, "metrics": metrics, "basis": basis, "count": len(basis)}
            cache[ck] = {"as_of": today, "data": data}
            try:
                config.POLYGON_FUND_CACHE_PATH.write_text(json.dumps(cache))
            except Exception:
                pass
    return data


def valuation_bars(prices: list[dict], basis: list[dict], gran: str = "annual") -> dict:
    """Stock Price + P/E + P/S bars at `gran` (annual|quarterly|monthly).

    `prices`: monthly [{"date":"YYYY-MM","close":..}] (Schwab). `basis`: annual
    [{date,eps,revenue,shares}] forward-filled to each price point (P/E, P/S).
    """
    if not prices:
        return {}
    basis = basis or []   # no earnings basis -> Stock Price still renders; P/E & P/S stay null

    # aggregate monthly prices to the requested granularity
    if gran == "monthly":
        pts = [{"ym": p["date"], "close": p["close"], "label": _month_lbl(p["date"])} for p in prices]
    elif gran == "quarterly":
        byq = {}
        for p in prices:  # last close per quarter wins (quarter-end)
            y, m = p["date"].split("-")
            q = (int(m) - 1) // 3 + 1
            byq[f"{y}-{q}"] = {"ym": p["date"], "close": p["close"], "label": f"Q{q}'{y[2:]}"}
        pts = [byq[k] for k in sorted(byq, key=lambda k: (k.split("-")[0], k.split("-")[1]))]
    else:  # annual
        byy = {}
        for p in prices:
            y = p["date"][:4]
            byy[y] = {"ym": p["date"], "close": p["close"], "label": f"'{y[2:]}"}
        pts = [byy[y] for y in sorted(byy)]

    b_sorted = sorted((b for b in basis if b.get("date")), key=lambda b: b["date"])

    def recent(ym):
        cutoff = ym + "-31"
        chosen = None
        for b in b_sorted:
            if b["date"] <= cutoff:
                chosen = b
            else:
                break
        return chosen

    price_d, pe_d, ps_d = [], [], []
    for pt in pts:
        px = pt["close"]
        b = recent(pt["ym"]) or {}
        eps, rev, shs = b.get("eps"), b.get("revenue"), b.get("shares")
        L = pt["label"]
        price_d.append({"label": L, "v": px})
        pe_d.append({"label": L, "v": round(px / eps, 1) if (px and eps and eps > 0) else None})
        ps_d.append({"label": L, "v": round(px * shs / rev, 2) if (px and rev and shs and rev > 0) else None})

    price_name = "Stock Price" if gran == "monthly" else "Stock Price (period-end)"
    return {
        # price is continuous -> a line reads far better than bars
        price_name: {"unit": "price", "good": True, "kind": "line", "data": price_d},
        "P/E":      {"unit": "x", "good": True, "kind": "bar", "data": pe_d},
        "P/S":      {"unit": "x", "good": True, "kind": "bar", "data": ps_d},
    }
