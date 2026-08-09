"""FastAPI application for ChaiStreet Command.

Endpoints:
  GET /health         liveness + configured source
  GET /api/refresh    ON-DEMAND live read-only pull -> engine -> snapshot -> JSON
  GET /api/history    recent snapshot headline metrics
  GET /api/delta      day-over-day delta between the two most recent snapshots
  GET /               the dashboard (web/index.html)

There is NO scheduler and NO polling: a snapshot is written only when
/api/refresh is called (i.e. when the user clicks Refresh). Read-only throughout.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date as _date

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

import config
from analytics import (
    assignment_waterfall,
    positions_breakdown,
    recommend_covered_calls_multi,
    sector_analysis,
    summary,
)
from ingest import load_book, load_index_quotes
from ingest.universe import UNIVERSE, all_symbols, sector_of
from ingest.finnhub import enrich as finnhub_enrich
from ingest.fmp import (
    earnings_history as fmp_earnings_history,
    fundamentals as fmp_fundamentals,
    market_lines as fmp_market_lines,
    next_earnings as fmp_next_earnings,
    profile as fmp_profile,
)
from ingest.polygon import financials as poly_financials, valuation_bars as poly_valuation
from ingest.schwab_source import (
    batch_quotes,
    daily_closes,
    fetch_call_chains,
    fetch_full_chain,
    fetch_put_chain,
    fetch_ytd_external_flows,
    instrument_fundamentals,
    monthly_closes,
)
from analytics.csp_selector import recommend_csps_multi
from analytics import gex as gexmod
from analytics.gex import compute_gex
from analytics.screener import run_screen
from store import latest_delta, latest_snapshots, write_snapshot

app = FastAPI(title="s3nthl portfolio dashboard", version="0.5.0")

# Recession Monitor — self-contained macro module, mounted under /api/recession.
try:
    from recession.api import router as _recession_router
    app.include_router(_recession_router)
except Exception as _exc:  # never let the macro module break the wheel app
    print(f"[recession] router not mounted: {_exc}")

WEB_DIR = config.BASE_DIR / "web"


def _serialize_delta(d: dict) -> dict:
    """Convert Delta dataclasses inside a latest_delta() result to plain dicts."""
    if "deltas" in d:
        return {**d, "deltas": {k: asdict(v) for k, v in d["deltas"].items()}}
    return d


@app.get("/health")
def health() -> dict:
    """Liveness probe. Reports which data source is configured."""
    return {"status": "ok", "source": config.CHAI_SOURCE}


@app.get("/api/refresh")
def api_refresh() -> JSONResponse:
    """On-demand pull: fetch the live book, compute everything, persist a snapshot."""
    try:
        book = load_book()  # source per CHAI_SOURCE; schwab -> snapshot fallback
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"error": "book_load_failed", "detail": str(exc)},
        )

    # Live VIX/VXN from Schwab (= ToS feed); fall back to stubs if unavailable.
    idx = {}
    if config.CHAI_SOURCE == "schwab":
        try:
            idx = load_index_quotes()
        except Exception:
            idx = {}
    # YTD P/L (if a start-of-year net liq baseline is configured)
    ytd_start = config.CHAI_YTD_START_NETLIQ
    ytd_flows = 0.0
    if config.CHAI_SOURCE == "schwab" and ytd_start > 0:
        try:
            ytd_flows = fetch_ytd_external_flows(config.CHAI_YTD_START_DATE)
        except Exception:
            ytd_flows = 0.0
    # P/L DAY = Schwab's currentDayProfitLoss (already on book.balances.day_pl) —
    # the exact "Day P/L" thinkorswim shows, so it reconciles against the broker.
    # (Pre-market it reflects the prior session, same as ToS.)
    s = summary(book, vxn=idx.get("VXN"), vix=idx.get("VIX"),
                ytd_start_netliq=ytd_start, ytd_flows=ytd_flows)
    s["day_pl_basis"] = "Schwab Day P/L (matches thinkorswim)"
    waterfall = asdict(assignment_waterfall(book, margin_rate=config.CHAI_MARGIN_RATE))
    breakdown = positions_breakdown(book)

    # Live sector/beta enrichment (cached); never let it break the refresh.
    underlyings = {p.symbol for p in book.stocks} | {o.symbol for o in book.options}
    try:
        live = finnhub_enrich(underlyings)
    except Exception:
        live = None
    sectors = sector_analysis(book, live=live)

    # Next earnings date per underlying (FMP, cached daily) -> {sym: {date,days,imminent,eps_est}}
    earnings = {}
    if config.CHAI_SOURCE == "schwab":
        try:
            for sym, v in fmp_next_earnings(underlyings).items():
                if v and v.get("date"):
                    days = (_date.fromisoformat(v["date"]) - book.as_of).days
                    earnings[sym] = {
                        "date": v["date"], "days": days,
                        "imminent": 0 <= days <= config.EARNINGS_ALERT_DAYS,
                        "eps_est": v.get("eps_est"),
                        "confirmed": v.get("confirmed", False),
                    }
        except Exception:
            earnings = {}

    row_id = write_snapshot(book, source=config.CHAI_SOURCE)
    delta = _serialize_delta(latest_delta())

    return JSONResponse({
        "snapshot_id": row_id,
        "source": config.CHAI_SOURCE,
        "as_of": s["as_of"],
        "summary": s,
        "waterfall": waterfall,
        "breakdown": breakdown,
        "sectors": sectors,
        "earnings": earnings,
        "delta": delta,
    })


@app.get("/api/covered-calls")
def api_covered_calls() -> JSONResponse:
    """Covered-call recommendations from live chains (lazy — its own endpoint)."""
    try:
        book = load_book()
    except Exception as exc:
        return JSONResponse(status_code=502,
                            content={"error": "book_load_failed", "detail": str(exc)})

    # eligible = holdings with >= 100 shares; fetch chains only for those.
    eligible = sorted({s.symbol for s in book.stocks if s.qty >= 100})
    chains = {}
    if config.CHAI_SOURCE == "schwab" and eligible:
        try:
            chains = fetch_call_chains(eligible)
        except Exception:
            chains = {}
    return JSONResponse(recommend_covered_calls_multi(book, chains))


@app.get("/api/csp-selector")
def api_csp_selector(symbol: str, contracts: int = 1) -> JSONResponse:
    """Cash-secured-put candidates for one ticker across DTE buckets & risk tiers.

    Lazy (its own endpoint) — one live PUT-chain fetch feeds every DTE bucket.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return JSONResponse({"error": "no_symbol"}, status_code=400)
    if config.CHAI_SOURCE != "schwab":
        return JSONResponse({"symbol": sym, "status": "offline", "buckets": []})
    try:
        chain = fetch_put_chain(sym)
        contracts = max(1, min(int(contracts or 1), 100))
        return JSONResponse(recommend_csps_multi(chain, sym, contracts=contracts))
    except Exception as exc:
        return JSONResponse(status_code=502,
                            content={"error": "csp_failed", "detail": str(exc)})


# GEX chain cache — the full chain is heavy; cache it briefly so expiry toggles
# recompute instantly without re-hitting Schwab. Keyed by symbol, TTL ~5 min.
_GEX_CHAIN_CACHE: dict = {}
_GEX_TTL = 300


def _gex_chain(sym: str) -> dict:
    import time
    now = time.time()
    hit = _GEX_CHAIN_CACHE.get(sym)
    if hit and now - hit[0] < _GEX_TTL and hit[1].get("underlying"):
        return hit[1]
    chain = fetch_full_chain(sym)
    if chain.get("underlying"):
        _GEX_CHAIN_CACHE[sym] = (now, chain)
    return chain


@app.get("/api/gex")
def api_gex(symbol: str, expiry: str = "all") -> JSONResponse:
    """Gamma-exposure board for one underlying, computed from the live Schwab
    chain. `expiry` = 'all' or a single 'YYYY-MM-DD'. Chain is cached ~5 min so
    the expiry toggle is instant."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return JSONResponse({"error": "no_symbol"}, status_code=400)
    if config.CHAI_SOURCE != "schwab":
        return JSONResponse({"symbol": sym, "status": "offline"})
    try:
        chain = _gex_chain(sym)
        if not chain.get("underlying"):
            return JSONResponse({"symbol": sym, "status": "no_chain"})
        # liquidity gate: illiquid names give misleading walls -> say so instead
        if gexmod.chain_total_oi(chain) < gexmod.MIN_CHAIN_OI:
            return JSONResponse({"symbol": sym, "status": "insufficient",
                                 "spot": chain.get("underlying")})
        exp_filter = None if (not expiry or expiry == "all") else [expiry]
        res = compute_gex(chain, sym, expiries=exp_filter)
        payload = asdict(res)
        payload.update(status="ok", regime=res.regime, flip_pct=res.flip_pct,
                       bias=gexmod.bias_read(res))
        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse(status_code=502,
                            content={"error": "gex_failed", "detail": str(exc)})


@app.get("/api/gex-walls")
def api_gex_walls(symbols: str) -> JSONResponse:
    """Compact call/put walls + bias for many tickers at once (holdings scan).

    Uses a LIGHTER chain fetch (near-dated, fewer strikes) so a whole book of
    names returns in seconds. Same wall/flip/bias method as the full GEX board.
    """
    from concurrent.futures import ThreadPoolExecutor
    syms = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()][:30]
    if not syms:
        return JSONResponse({"error": "no_symbols"}, status_code=400)
    if config.CHAI_SOURCE != "schwab":
        return JSONResponse({"status": "offline", "walls": []})

    def one(sym: str) -> dict:
        try:
            chain = fetch_full_chain(sym, from_days=0, to_days=45, strike_count=80)
            if not chain.get("underlying"):
                return {"symbol": sym, "status": "no_chain"}
            if gexmod.chain_total_oi(chain) < gexmod.MIN_CHAIN_OI:
                return {"symbol": sym, "status": "insufficient",
                        "spot": round(chain["underlying"], 2)}
            res = compute_gex(chain, sym)
            return {"symbol": sym, "status": "ok", "spot": res.spot,
                    "net_gex": res.net_gex, "regime": res.regime,
                    "call_wall": res.call_wall, "put_wall": res.put_wall,
                    "call_wall_weak": res.call_wall_weak,
                    "put_wall_weak": res.put_wall_weak, "flip": res.flip,
                    "bias": gexmod.bias_read(res)}
        except Exception as exc:
            return {"symbol": sym, "status": "error", "detail": str(exc)[:120]}

    with ThreadPoolExecutor(max_workers=min(8, len(syms))) as ex:
        walls = list(ex.map(one, syms))
    return JSONResponse({"status": "ok", "walls": walls})


@app.get("/api/fundamentals")
def api_fundamentals(symbol: str, period: str = "annual") -> JSONResponse:
    """Statement time-series for one ticker. period = annual | quarterly | monthly.

    Statements: annual (yearly) or quarterly. Valuation (Stock Price, P/E, P/S):
    annual/quarterly/monthly. Polygon (deep) primary + Schwab price + FMP extras.
    """
    if config.CHAI_SOURCE != "schwab" or not config.FMP_API_KEY:
        return JSONResponse({"symbol": symbol.upper(), "period": period, "metrics": {},
                             "error": "no_fmp_key"})
    gran = period if period in ("annual", "quarterly", "monthly") else "annual"
    stmt_tf = "annual" if gran == "annual" else "quarterly"
    fmp_period = "annual" if gran == "annual" else "quarter"
    try:
        poly = poly_financials(symbol, stmt_tf) if config.POLYGON_API_KEY else {"metrics": {}, "basis": []}

        if poly.get("metrics"):
            # valuation basis is ALWAYS annual (deep, forward-filled to each price point)
            poly_ann = poly if stmt_tf == "annual" else poly_financials(symbol, "annual")
            prices = monthly_closes(symbol, years=15)
            val = poly_valuation(prices, poly_ann.get("basis") or [], gran)
            metrics = {**val, **poly["metrics"]}   # all-Polygon (+ Schwab price) = uniform depth
            # Re-add EBITDA + Free Cash Flow: Polygon's standardized statements omit
            # them, so FMP supplies them — but only ~5 periods deep, so badge that
            # shorter depth honestly rather than let it masquerade as 16-year data.
            try:
                fx = (fmp_fundamentals(symbol, fmp_period).get("metrics") or {})
                badge = "5y" if gran == "annual" else "5q"
                for nm in ("EBITDA", "Free Cash Flow"):
                    mm = fx.get(nm)
                    if mm and any(x.get("v") is not None for x in mm.get("data", [])):
                        metrics[nm] = {**mm, "badge": badge}
            except Exception:
                pass
            metrics = {k: v for k, v in metrics.items()   # drop fields with no data for this ticker
                       if any(x.get("v") is not None for x in v.get("data", []))}
            n_years = poly_ann.get("count", poly.get("count", 0))
            return JSONResponse({"symbol": poly["symbol"], "period": gran, "source": "polygon",
                                 "years": n_years, "young": (n_years or 0) < 3,
                                 "metrics": metrics, "core_years": poly.get("count", 0)})

        # Polygon has no deep coverage (foreign/ETF/new) -> FMP statements fallback.
        fmp = fmp_fundamentals(symbol, fmp_period)
        if fmp.get("metrics"):
            val = fmp_market_lines(monthly_closes(symbol), fmp.get("basis") or [])
            return JSONResponse({"symbol": symbol.upper(), "period": gran, "source": "fmp",
                                 "years": fmp.get("count", 0), "young": (fmp.get("count", 0) or 0) < 3,
                                 "metrics": {**val, **fmp["metrics"]}})

        # Tier 3 — no deep statements (foreign 20-F filer / gated / brand-new listing),
        # but it's still a real company: show the price history + a profile snapshot
        # rather than a dead-end. Both come from sources that DO cover it (Schwab price,
        # FMP profile), so the user always gets something useful.
        # Schwab instrument fundamentals are primary (real-time, no external limit);
        # FMP profile enriches with sector/industry/employees when its quota allows.
        prof = dict(fmp_profile(symbol) or {})
        schwab_fun = instrument_fundamentals(symbol)
        for k, v in schwab_fun.items():
            if v is not None and prof.get(k) in (None, "", 0):
                prof[k] = v
        prices = monthly_closes(symbol, years=15)
        price_metric = poly_valuation(prices, [], gran)  # Stock Price line only
        price_metric = {k: v for k, v in price_metric.items()
                        if any(x.get("v") is not None for x in v.get("data", []))}
        # months of price history -> "young" if under ~3 years (drives the note + default view)
        span_m = 0
        ds = [p["date"] for p in prices if p.get("date")]
        if len(ds) >= 2:
            (y0, m0), (y1, m1) = (ds[0].split("-"), ds[-1].split("-"))
            span_m = (int(y1) - int(y0)) * 12 + (int(m1) - int(m0))
        if price_metric or prof:
            return JSONResponse({"symbol": symbol.upper(), "period": gran, "source": "snapshot",
                                 "years": round(span_m / 12, 1), "young": span_m < 36,
                                 "profile": prof, "metrics": price_metric, "no_statements": True})

        return JSONResponse({"symbol": symbol.upper(), "period": gran, "source": "none",
                             "years": 0, "metrics": {}})
    except Exception as exc:
        return JSONResponse(status_code=502,
                            content={"error": "fundamentals_failed", "detail": str(exc)})


_MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _earn_label(iso: str) -> str:
    """'2026-08-04' -> \"Aug '26\"."""
    try:
        y, m, _ = iso.split("-")
        return f"{_MON[int(m)]} '{y[2:]}"
    except Exception:
        return iso


def _earnings_moves(rows: list[dict], daily: list[dict]) -> list[dict]:
    """Price reaction around each earnings date, from daily closes.

    Uses the report time when FMP provides it: 'bmo' (before open) -> that day's
    session reacts; 'amc' (after close) -> the next session reacts; unknown ->
    bracket the report (prior close -> next close). Returns [{date, move}] %.
    """
    import bisect
    if not daily:
        return []
    dates = [d["date"] for d in daily]
    close = {d["date"]: d["close"] for d in daily}

    def prev_close(dt):                       # last trading day strictly before dt
        i = bisect.bisect_left(dates, dt) - 1
        return close[dates[i]] if i >= 0 else None

    def on_or_after(dt):                       # first trading day >= dt
        i = bisect.bisect_left(dates, dt)
        return close[dates[i]] if i < len(dates) else None

    def strictly_after(dt):                    # first trading day > dt
        i = bisect.bisect_right(dates, dt)
        return close[dates[i]] if i < len(dates) else None

    out = []
    for r in rows:
        dt = r["date"]
        t = (r.get("time") or "").lower()
        if t == "bmo":
            c0, c1 = prev_close(dt), on_or_after(dt)
        elif t == "amc":
            c0, c1 = on_or_after(dt), strictly_after(dt)
        else:
            c0, c1 = prev_close(dt), strictly_after(dt)
        if c0 and c1 and c0 > 0:
            out.append({"date": dt, "move": round((c1 / c0 - 1) * 100, 2)})
    return out


@app.get("/api/earnings-detail")
def api_earnings_detail(symbol: str) -> JSONResponse:
    """Reported EPS history + the stock's price reaction to each report.

    Lazy companion to /api/fundamentals — powers the two Earnings cards. EPS from
    FMP (actual vs estimate); the move is computed from Schwab daily closes.
    """
    if config.CHAI_SOURCE != "schwab" or not config.FMP_API_KEY:
        return JSONResponse({"symbol": symbol.upper(), "eps": [], "moves": [], "status": "no_key"})
    try:
        hist = fmp_earnings_history(symbol)
        if hist is None:      # provider unreachable (rate-limited) -> tell the UI to wait
            return JSONResponse({"symbol": symbol.upper(), "eps": [], "moves": [],
                                 "status": "pending"})
        today = _date.today().isoformat()
        past = [r for r in hist if r["date"] <= today and r.get("eps_actual") is not None][-12:]
        daily = daily_closes(symbol, years=5)

        eps = []
        for r in past:
            a, e = r.get("eps_actual"), r.get("eps_est")
            beat = (a >= e) if (a is not None and e is not None) else None
            surprise = round((a - e) / abs(e) * 100, 1) if (a is not None and e not in (None, 0)) else None
            eps.append({"label": _earn_label(r["date"]), "date": r["date"],
                        "actual": a, "est": e, "beat": beat, "surprise": surprise})

        moves = [{"label": _earn_label(m["date"]), "date": m["date"], "move": m["move"]}
                 for m in _earnings_moves(past, daily)]
        if eps or moves:
            return JSONResponse({"symbol": symbol.upper(), "eps": eps, "moves": moves,
                                 "status": "ok", "source": "fmp"})

        # FMP has no reported history for this ticker (gated 402 / not covered).
        # Fall back to Polygon's quarterly EPS — reported (split-adjusted) figures,
        # but no analyst estimate (so no beat/miss) and no reliable announcement date
        # (so no price-reaction). Honest partial: reported EPS only.
        if config.POLYGON_API_KEY:
            pq = poly_financials(symbol, "quarterly")
            series = ((pq.get("metrics") or {}).get("EPS (diluted)") or {}).get("data", [])
            pts = [x for x in series if x.get("v") is not None][-12:]
            if pts:
                eps = [{"label": x["label"], "date": None, "actual": x["v"],
                        "est": None, "beat": None, "surprise": None} for x in pts]
                return JSONResponse({"symbol": symbol.upper(), "eps": eps, "moves": [],
                                     "status": "ok", "source": "polygon",
                                     "note": "reported EPS from Polygon — estimates (beat/miss) and "
                                             "price-reaction aren't available for this ticker"})

        return JSONResponse({"symbol": symbol.upper(), "eps": [], "moves": [], "status": "none"})
    except Exception as exc:
        return JSONResponse({"symbol": symbol.upper(), "eps": [], "moves": [],
                             "status": "error", "error": str(exc)})


def _latest_good(metrics: dict, name: str):
    """Latest non-null value of a metric series, or None."""
    m = (metrics or {}).get(name)
    if not m:
        return None
    pts = [x["v"] for x in m.get("data", []) if x.get("v") is not None]
    return pts[-1] if pts else None


def _ticker_weakness(sym: str) -> dict | None:
    """Fundamental red-flags for one ticker from daily-cached data: a trailing
    net loss and/or negative free cash flow. Returns None if healthy or unknown.
    Net income comes from Polygon (deep) with an FMP fallback; FCF is FMP-only
    (Polygon doesn't carry the line). Never raises."""
    reasons = []
    ni = fcf = None
    try:
        if config.POLYGON_API_KEY:
            ni = _latest_good(poly_financials(sym, "annual").get("metrics"), "Net Income")
        fmp = fmp_fundamentals(sym, "annual").get("metrics") if config.FMP_API_KEY else {}
        if ni is None:
            ni = _latest_good(fmp, "Net Income")
        fcf = _latest_good(fmp, "Free Cash Flow")
    except Exception:
        return None
    if ni is not None and ni < 0:
        reasons.append("net loss")
    if fcf is not None and fcf < 0:
        reasons.append("negative FCF")
    return {"reasons": reasons} if reasons else None


@app.get("/api/weakflags")
def api_weakflags() -> JSONResponse:
    """Per-holding fundamental red-flags (net loss / negative FCF), cached daily.

    Lazy companion to /api/refresh — the Portfolio % tab calls this after render so
    the (first-of-day, slower) fundamentals warm-up never blocks the headline board.
    """
    if config.CHAI_SOURCE != "schwab" or not config.FMP_API_KEY:
        return JSONResponse({"flags": {}})
    try:
        book = load_book()
    except Exception as exc:
        return JSONResponse(status_code=502,
                            content={"error": "book_load_failed", "detail": str(exc)})
    from concurrent.futures import ThreadPoolExecutor

    syms = sorted({s.symbol for s in book.stocks})
    flags = {}
    try:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for sym, w in zip(syms, ex.map(_ticker_weakness, syms)):
                if w:
                    flags[sym] = w
    except Exception:
        flags = {}
    return JSONResponse({"flags": flags})


@app.get("/api/screener")
def api_screener(force: bool = False, cached_only: bool = False) -> JSONResponse:
    """Fundamental research screen over the curated universe (Polygon + prices).

    Heavy (deep financials for ~90 names). ON-DEMAND ONLY: `cached_only=1` serves
    whatever is cached without ever calling out (opening the tab costs nothing);
    `force=1` explicitly rebuilds (the only path that hits the APIs). This keeps us
    well clear of rate limits — a rebuild happens only when the user asks for one.
    """
    import json as _json
    from concurrent.futures import ThreadPoolExecutor

    today = _date.today().isoformat()
    cached = None
    try:
        cached = _json.loads(config.SCREENER_CACHE_PATH.read_text())
    except Exception:
        cached = None

    if cached and not force:
        data = dict(cached["data"])
        data["cached_as_of"] = cached.get("as_of")
        data["stale"] = cached.get("as_of") != today
        return JSONResponse(data)

    if cached_only:            # never build on a passive open — ask the user to run it
        return JSONResponse({"status": "empty"})

    if config.CHAI_SOURCE != "schwab" or not config.POLYGON_API_KEY:
        return JSONResponse({"error": "screener_unavailable",
                             "detail": "needs live Schwab + Polygon"}, status_code=503)

    syms = all_symbols()
    prices = batch_quotes(syms)
    fins: dict[str, dict] = {}
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for sym, fin in zip(syms, ex.map(lambda s: poly_financials(s, "annual"), syms)):
                fins[sym] = fin
    except Exception:
        pass

    data_by_symbol = {}
    for sym in syms:
        fin = fins.get(sym) or {}
        if fin.get("metrics"):
            data_by_symbol[sym] = {"fin": fin, "price": prices.get(sym), "sector": sector_of(sym)}

    result = run_screen(data_by_symbol)
    result["as_of"] = today
    result["universe_size"] = len(syms)
    try:
        config.SCREENER_CACHE_PATH.write_text(_json.dumps({"as_of": today, "data": result}))
    except Exception:
        pass
    return JSONResponse(result)


@app.get("/api/company-brief")
def api_company_brief(symbol: str) -> JSONResponse:
    """AI 'what is this company' snapshot for the Fundamentals tab.

    Served from a durable cache (30-day TTL) — pre-warmed for holdings + megacaps,
    so most lookups are instant; a cold ticker generates once and is cached.
    """
    from ingest.ai import get_brief, provider
    sym = (symbol or "").strip().upper()
    if not sym:
        return JSONResponse({"error": "no_symbol"}, status_code=400)
    if not provider():
        return JSONResponse({"status": "no_ai"})
    return JSONResponse(get_brief(sym))


@app.get("/api/brief-warm")
def api_brief_warm(force: bool = False) -> JSONResponse:
    """Pre-generate AI briefs for all holdings + the top-50 megacaps (cached 30d).

    On-demand: this is the only path that spends AI credits in bulk. Returns the
    warm summary. Safe to re-run — already-fresh tickers are skipped.
    """
    from ingest.ai import warm_briefs, provider
    from ingest.universe import MEGACAP_50
    if not provider():
        return JSONResponse({"status": "no_ai"})
    holdings = []
    try:
        book = load_book()
        holdings = sorted({s.symbol for s in book.stocks} | {o.symbol for o in book.options})
    except Exception:
        holdings = []
    res = warm_briefs(list(holdings) + list(MEGACAP_50), force=force)
    res["holdings"] = len(holdings)
    return JSONResponse(res)


@app.get("/api/ai-status")
def api_ai_status() -> JSONResponse:
    """Whether the Ask tab is wired (a key is present) + which model/provider."""
    from ingest.ai import provider, active_model
    prov = provider()
    return JSONResponse({"enabled": bool(prov), "provider": prov, "model": active_model()})


@app.post("/api/ask")
async def api_ask(request: Request) -> JSONResponse:
    """Answer a question about the live dashboard via Claude.

    Body: {messages:[{role,content}...], context:{...refresh payload...}}. The
    context is the data the page is showing, so answers reflect exactly that.
    """
    from ingest.ai import ask as ai_ask
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request", "detail": "invalid JSON"}, status_code=400)
    res = ai_ask(body.get("messages") or [], body.get("context") or {})
    status = 200 if "answer" in res else (401 if res.get("error") == "auth" else 200)
    return JSONResponse(res, status_code=status)


@app.get("/api/history")
def api_history(n: int = 30) -> JSONResponse:
    """Recent snapshot headline metrics, newest first."""
    snaps = latest_snapshots(n)
    return JSONResponse({
        "count": len(snaps),
        "snapshots": [
            {"id": s.id, "refreshed_at": s.refreshed_at, "as_of": s.as_of,
             "source": s.source, **s.metrics}
            for s in snaps
        ],
    })


@app.get("/api/delta")
def api_delta() -> JSONResponse:
    """Day-over-day delta between the two most recent snapshots."""
    return JSONResponse(_serialize_delta(latest_delta()))


@app.get("/")
def index() -> FileResponse:
    """Serve the dashboard."""
    return FileResponse(WEB_DIR / "index.html")


@app.get("/recession")
def recession_page() -> FileResponse:
    """Serve the standalone macro / recession research page."""
    return FileResponse(config.BASE_DIR / "recession" / "web" / "index.html")


def main() -> None:
    """Run the dev server: `python -m api.app` or `uvicorn api.app:app`."""
    import uvicorn

    uvicorn.run("api.app:app", host=config.CHAI_HOST, port=config.CHAI_PORT, reload=False)


if __name__ == "__main__":
    main()
