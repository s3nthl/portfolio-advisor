"""Financial Modeling Prep — next earnings date per underlying, cached daily.

Read-only external calls to financialmodelingprep.com. Only the NEXT scheduled
earnings date is returned. Cached to a local JSON file (1-day TTL) so refreshes
stay fast and within the free-tier rate limit. Never raises: on any failure a
symbol simply has no earnings date.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import config

_BASE = "https://financialmodelingprep.com/stable"


def _load_cache() -> dict:
    try:
        return json.loads(config.FMP_CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        config.FMP_CACHE_PATH.write_text(json.dumps(cache, indent=0))
    except Exception:
        pass


def _pick_next(rows, today_iso: str):
    """From FMP earnings rows, pick the soonest earnings dated today or later.

    A future row hasn't reported yet (epsActual is null), which is exactly the
    "next earnings" we want. Ties on date keep the first — FMP returns one row
    per scheduled report, so ties are rare.
    """
    future = sorted(
        (r for r in rows if r.get("date") and r["date"] >= today_iso),
        key=lambda r: r["date"],
    )
    if not future:
        return None
    r = future[0]
    return {"date": r["date"], "eps_est": r.get("epsEstimated"),
            "confirmed": r.get("epsActual") is None and r.get("revenueActual") is None}


# retained name for callers/tests that referenced the old helper
_next_from_payload = _pick_next


def _bulk_earnings_calendar(key: str, frm: str, to: str, retries: int = 4):
    """One call for the whole earnings calendar in [frm, to]. Returns a list on
    success (possibly empty), or None on failure (429/error) — so the caller can
    tell "no earnings" apart from "couldn't fetch" and never caches a failure."""
    import httpx
    import time as _t
    for i in range(retries):
        try:
            r = httpx.get(f"{_BASE}/earnings-calendar",
                          params={"from": frm, "to": to, "apikey": key}, timeout=25.0)
            if r.status_code == 200:
                j = r.json()
                return j if isinstance(j, list) else None
            if r.status_code == 429 and i < retries - 1:
                _t.sleep(1.2 + 1.3 * i)   # rate-limited: back off and retry
                continue
            return None                    # 402/403/other -> give up (caller falls back)
        except Exception:
            _t.sleep(1.0)
    return None


def _fill_per_symbol(syms, key, today_iso, cache) -> None:
    """Per-symbol earnings lookup, used only for the few tickers the bulk calendar
    didn't cover. Caches ONLY definitive successes (never poisons on failure)."""
    import httpx
    from concurrent.futures import ThreadPoolExecutor
    client = httpx.Client(timeout=12.0)
    _FAIL = object()

    def one(s: str):
        try:
            r = client.get(f"{_BASE}/earnings", params={"symbol": s, "apikey": key})
            if r.status_code == 200 and isinstance(r.json(), list):
                return s, _pick_next(r.json(), today_iso)   # success (maybe None = truly none)
        except Exception:
            pass
        return s, _FAIL

    try:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for s, res in ex.map(one, syms):
                if res is _FAIL:
                    continue                                 # don't cache a failure
                cache[s] = {"as_of": today_iso, "next": res}
    finally:
        client.close()


def profile(symbol: str, *, force: bool = False) -> dict:
    """Company profile snapshot (market cap, beta, 52-wk range, sector, etc.).

    Works on the FMP free tier even for foreign filers whose STATEMENTS are gated,
    so it powers a graceful fallback page when deep financials aren't available.
    Cached daily. Retries a couple of times (FMP occasionally returns an empty body
    under burst). Never raises: returns {} on failure.
    """
    sym = symbol.upper()
    ck = f"{sym}:PROFILE"
    today = date.today().isoformat()
    try:
        cache = json.loads(config.FMP_FUND_CACHE_PATH.read_text())
    except Exception:
        cache = {}
    if not force and cache.get(ck, {}).get("as_of") == today:
        return cache[ck]["data"]

    key = config.FMP_API_KEY
    data = {}
    if key:
        import httpx
        import time as _t
        for attempt in range(3):
            try:
                r = httpx.get(f"{_BASE}/profile", params={"symbol": sym, "apikey": key}, timeout=15.0)
                if r.status_code == 200:
                    j = r.json()
                    if isinstance(j, list) and j:
                        d = j[0]
                        data = {
                            "name": d.get("companyName"), "sector": d.get("sector"),
                            "industry": d.get("industry"), "country": d.get("country"),
                            "exchange": d.get("exchange"), "price": d.get("price"),
                            "market_cap": d.get("marketCap"), "beta": d.get("beta"),
                            "range_52w": d.get("range"), "employees": d.get("fullTimeEmployees"),
                            "ceo": d.get("ceo"), "ipo_date": d.get("ipoDate"),
                            "last_dividend": d.get("lastDividend"),
                            "is_etf": bool(d.get("isEtf")), "is_fund": bool(d.get("isFund")),
                            "description": (d.get("description") or "")[:320],
                        }
                        break
            except Exception:
                pass
            _t.sleep(0.8 * (attempt + 1))
    if data:
        cache[ck] = {"as_of": today, "data": data}
        try:
            config.FMP_FUND_CACHE_PATH.write_text(json.dumps(cache))
        except Exception:
            pass
    return data


def next_earnings(symbols, *, force: bool = False) -> dict[str, dict | None]:
    """Return {symbol: {"date":"YYYY-MM-DD","time":...} | None} for `symbols`."""
    cache = _load_cache()
    symbols = sorted({s.upper() for s in symbols})
    key = config.FMP_API_KEY
    today = date.today().isoformat()

    to_fetch = [s for s in symbols
                if force or cache.get(s, {}).get("as_of") != today]

    if to_fetch and key:
        need = set(to_fetch)
        # One bulk calendar call covers ~all holdings' next report (well within a
        # free-tier request budget). A ~7-month window catches every quarterly
        # reporter's next date with margin.
        horizon = (date.today() + timedelta(days=210)).isoformat()
        rows = _bulk_earnings_calendar(key, today, horizon)

        if rows is not None:                              # bulk succeeded (self-heals)
            by_sym: dict[str, list] = {}
            for r in rows:
                s = (r.get("symbol") or "").upper()
                if s in need:
                    by_sym.setdefault(s, []).append(r)
            missing = []
            for s in to_fetch:
                nxt = _pick_next(by_sym.get(s, []), today)
                if nxt:
                    cache[s] = {"as_of": today, "next": nxt}   # confirmed hit
                else:
                    missing.append(s)                          # not in window -> verify directly
            # Verify the stragglers per-symbol so we never *silently* report "no
            # earnings" for a name that simply reports beyond the bulk window.
            if missing:
                _fill_per_symbol(missing, key, today, cache)
            _save_cache(cache)
        # else: bulk fetch FAILED (429/network). Do NOT write anything — the day's
        # cache is left intact so the next refresh retries. No poisoning.

    return {s: cache.get(s, {}).get("next") for s in symbols}


def earnings_history(symbol: str, *, force: bool = False):
    """Reported-earnings history for one symbol (actual & estimated EPS per quarter).

    From FMP /stable/earnings (past rows carry epsActual). Cached daily; only
    successful fetches are cached. Returns:
      * a list of rows on success (possibly empty = the ticker has no earnings),
      * None when the provider couldn't be reached (rate-limited / error) and no
        same-day cache exists — so callers can say "pending" vs "none". Never poisons.
    Rows: {date, time, eps_actual, eps_est, rev_actual, rev_est}, oldest->newest.
    """
    sym = symbol.upper()
    ck = f"{sym}::HIST"
    today = date.today().isoformat()
    cache = _load_cache()
    if not force and cache.get(ck, {}).get("as_of") == today:
        return cache[ck]["rows"]

    key = config.FMP_API_KEY
    if not key:
        return cache.get(ck, {}).get("rows")   # list if cached, else None

    import httpx
    import time as _t
    for i in range(4):
        try:
            r = httpx.get(f"{_BASE}/earnings", params={"symbol": sym, "apikey": key}, timeout=15.0)
            if r.status_code == 200 and isinstance(r.json(), list):
                rows = []
                for x in r.json():
                    if not x.get("date"):
                        continue
                    rows.append({
                        "date": x["date"], "time": x.get("time"),
                        "eps_actual": x.get("epsActual"), "eps_est": x.get("epsEstimated"),
                        "rev_actual": x.get("revenueActual"), "rev_est": x.get("revenueEstimated"),
                    })
                rows.sort(key=lambda z: z["date"])
                cache[ck] = {"as_of": today, "rows": rows}
                _save_cache(cache)
                return rows
            if r.status_code == 429 and i < 3:
                _t.sleep(1.2 + 1.3 * i)
                continue
            if r.status_code in (402, 403):
                # ticker is gated on this plan (foreign filer / newer name) — this is
                # PERMANENT, not a transient limit. Cache [] so we don't retry and so
                # the caller reports "not covered", not "rate-limited".
                cache[ck] = {"as_of": today, "rows": []}
                _save_cache(cache)
                return []
            break
        except Exception:
            _t.sleep(1.0)
    return cache.get(ck, {}).get("rows")   # None if nothing cached -> "provider unavailable" (retry)


# --------------------------------------------------------------------------- #
# Fundamentals — statement time-series for the Fundamentals tab (cached daily)
# --------------------------------------------------------------------------- #
_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _lbl(d: str) -> str:
    """'2026-06-30' -> \"Jun '26\"."""
    try:
        y, m, _ = d.split("-")
        return f"{_MONTHS[int(m)]} '{y[2:]}"
    except Exception:
        return d


def _num(row, key):
    v = row.get(key)
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _build_metrics(rows: list[dict]) -> dict:
    """rows: merged statement rows, oldest->newest. Returns {name:{unit,good,data}}."""
    def s(fn):
        return [{"label": _lbl(r["date"]), "v": fn(r)} for r in rows]

    def fcf(r):
        ocf = _num(r, "netCashProvidedByOperatingActivities")
        capex = _num(r, "investmentsInPropertyPlantAndEquipment")
        return (ocf + (capex or 0)) if ocf is not None else None

    def margin(r):
        rev, gp = _num(r, "revenue"), _num(r, "grossProfit")
        return (gp / rev * 100) if (rev and gp is not None) else None

    def total_debt(r):
        st, lt = _num(r, "shortTermDebt"), _num(r, "longTermDebt")
        return (st or 0) + (lt or 0) if (st is not None or lt is not None) else None

    metrics = {
        "Revenue":            {"unit": "$", "good": True,  "data": s(lambda r: _num(r, "revenue"))},
        "Gross Profit":       {"unit": "$", "good": True,  "data": s(lambda r: _num(r, "grossProfit"))},
        "Gross Margin":       {"unit": "%", "good": True,  "data": s(margin)},
        "EBITDA":             {"unit": "$", "good": True,  "data": s(lambda r: _num(r, "ebitda"))},
        "Operating Income":   {"unit": "$", "good": True,  "data": s(lambda r: _num(r, "operatingIncome"))},
        "Net Income":         {"unit": "$", "good": True,  "data": s(lambda r: _num(r, "netIncome"))},
        "EPS (diluted)":      {"unit": "eps", "good": True, "data": s(lambda r: _num(r, "epsDiluted"))},
        "Cash from Operations": {"unit": "$", "good": True, "data": s(lambda r: _num(r, "netCashProvidedByOperatingActivities"))},
        "Free Cash Flow":     {"unit": "$", "good": True,  "data": s(fcf)},
        "Capital Expenditure": {"unit": "$", "good": False, "data": s(lambda r: _num(r, "investmentsInPropertyPlantAndEquipment"))},
        "Cash & Equivalents": {"unit": "$", "good": True,  "data": s(lambda r: _num(r, "cashAndCashEquivalents"))},
        "Total Debt":         {"unit": "$", "good": False, "data": s(total_debt)},
    }
    for m in metrics.values():
        m["kind"] = "bar"
    return metrics


def _lbl_ym(ym: str) -> str:
    """'2026-07' -> \"Jul '26\"."""
    try:
        y, m = ym.split("-")
        return f"{_MONTHS[int(m)]} '{y[2:]}"
    except Exception:
        return ym


def market_lines(prices: list[dict], basis: list[dict]) -> dict:
    """Yearly Stock Price + computed P/E & P/S BARS (year-end close).

    `prices`: [{"date":"YYYY-MM","close":..}] (Schwab, monthly). Aggregated to one
    year-end close per calendar year. P/E, P/S use that year's price over the
    year's annual EPS/revenue (approx trailing valuation). All bars, one per year.
    """
    if not prices:
        return {}
    # last monthly close per calendar year -> year-end price
    by_year: dict[str, float] = {}
    for p in prices:  # oldest -> newest, so last write wins
        by_year[p["date"][:4]] = p["close"]
    years = sorted(by_year)
    basis_by_year = {b["date"][:4]: b for b in basis if b.get("date")}

    def yr_lbl(y):
        return f"'{y[2:]}"

    price_d = [{"label": yr_lbl(y), "v": by_year[y]} for y in years]
    pe_d, ps_d = [], []
    for y in sorted(basis_by_year):          # only years with statement data
        px, b = by_year.get(y), basis_by_year[y]
        eps, rev, shs = b.get("eps"), b.get("revenue"), b.get("shares")
        pe_d.append({"label": yr_lbl(y), "v": round(px / eps, 1) if (px and eps and eps > 0) else None})
        ps_d.append({"label": yr_lbl(y), "v": round(px * shs / rev, 2) if (px and rev and shs and rev > 0) else None})

    return {
        "Stock Price (yr-end)": {"unit": "price", "good": True, "kind": "line", "data": price_d},
        "P/E (approx)":         {"unit": "x", "good": True, "kind": "bar", "data": pe_d},
        "P/S (approx)":         {"unit": "x", "good": True, "kind": "bar", "data": ps_d},
    }


def fundamentals(symbol: str, period: str = "quarter", force: bool = False) -> dict:
    """Statement time-series for one symbol, cached daily per (symbol, period).

    Pulls income/cash-flow/balance-sheet (free tier), merges by report date, and
    builds ~12 metric series. Never raises: returns empty metrics on failure.
    """
    period = "quarter" if str(period).lower().startswith("q") else "annual"
    limit = config.FMP_STMT_LIMIT  # free tier caps at 5; raise on a paid plan
    sym = symbol.upper()
    ck = f"{sym}:{period}"
    today = date.today().isoformat()

    try:
        cache = json.loads(config.FMP_FUND_CACHE_PATH.read_text())
    except Exception:
        cache = {}
    if not force and cache.get(ck, {}).get("as_of") == today:
        return cache[ck]["data"]

    key = config.FMP_API_KEY
    data = {"symbol": sym, "period": period, "metrics": {}, "count": 0}
    if key:
        import httpx
        from concurrent.futures import ThreadPoolExecutor

        endpoints = ["income-statement", "cash-flow-statement", "balance-sheet-statement"]

        def fetch(ep):
            try:
                r = httpx.get(f"{_BASE}/{ep}",
                              params={"symbol": sym, "period": period, "limit": limit, "apikey": key},
                              timeout=20.0)
                return r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=3) as ex:
            payloads = list(ex.map(fetch, endpoints))

        merged: dict[str, dict] = {}
        for rows in payloads:
            for row in rows:
                d = row.get("date")
                if not d:
                    continue
                merged.setdefault(d, {}).update(row)
        ordered = sorted(merged.values(), key=lambda r: r["date"])
        if ordered:
            # valuation basis for computing P/E, P/S monthly lines (from statements)
            basis = [{"date": r["date"], "eps": _num(r, "epsDiluted"),
                      "revenue": _num(r, "revenue"),
                      "shares": _num(r, "weightedAverageShsOutDil")} for r in ordered]
            data = {"symbol": sym, "period": period,
                    "metrics": _build_metrics(ordered), "basis": basis,
                    "count": len(ordered)}
            cache[ck] = {"as_of": today, "data": data}
            try:
                config.FMP_FUND_CACHE_PATH.write_text(json.dumps(cache))
            except Exception:
                pass
    return data
