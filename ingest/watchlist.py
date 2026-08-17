"""Watchlist store + live enrichment.

A user-editable list of tickers to monitor (seeded from watchlist_seed.json,
their vetted list), with live price / today's move / dip-from-52wk-high and an
on-open dip-alert rule (a global % threshold plus per-ticker overrides). News is
lazy (Finnhub), fetched per ticker on demand.

Local-first: the live list lives in watchlist.json (gitignored, user-owned); the
seed is committed so a fresh clone starts populated. Read-only market data.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import config

_BASE = Path(__file__).resolve().parent.parent
_SEED = _BASE / "watchlist_seed.json"
_FILE = _BASE / "watchlist.json"
_DEFAULT_RULE = {"dip_pct": 15.0, "day_pct": 5.0}   # alert if >=15% below 52-wk high OR drops >=5% today
_NEWS_CACHE: dict = {}
_NEWS_TTL = 1800                            # 30 min


def _seed() -> dict:
    try:
        items = json.loads(_SEED.read_text())
    except Exception:
        items = []
    return {"rule": dict(_DEFAULT_RULE), "items": items}


def load() -> dict:
    try:
        d = json.loads(_FILE.read_text())
        d["rule"] = {**_DEFAULT_RULE, **(d.get("rule") or {})}   # backfill new rule keys
        d.setdefault("items", [])
        return d
    except Exception:
        d = _seed()
        save(d)
        return d


def save(d: dict) -> None:
    try:
        _FILE.write_text(json.dumps(d, indent=1))
    except Exception:
        pass


def add(symbol: str) -> dict:
    sym = (symbol or "").strip().upper()
    d = load()
    if sym and not any(i["symbol"] == sym for i in d["items"]):
        d["items"].insert(0, {"symbol": sym, "company": None, "sector": None,
                              "list": "custom", "portfolio": False})
        save(d)
    return d


def remove(symbol: str) -> dict:
    sym = (symbol or "").strip().upper()
    d = load()
    d["items"] = [i for i in d["items"] if i["symbol"] != sym]
    save(d)
    return d


def set_rule(dip_pct: float | None = None, day_pct: float | None = None) -> dict:
    d = load()
    if dip_pct is not None:
        d["rule"]["dip_pct"] = round(float(dip_pct), 1)
    if day_pct is not None:
        d["rule"]["day_pct"] = round(float(day_pct), 1)
    save(d)
    return d


def set_ticker_alert(symbol: str, pct: float | None) -> dict:
    """Per-ticker override of the dip threshold; pct=None clears it (use global)."""
    sym = (symbol or "").strip().upper()
    d = load()
    for i in d["items"]:
        if i["symbol"] == sym:
            if pct is None:
                i.pop("alert_pct", None)
            else:
                i["alert_pct"] = round(float(pct), 1)
    save(d)
    return d


def enriched() -> dict:
    """The list with live quotes + dip metrics + alert flags."""
    from ingest.schwab_source import rich_quotes
    d = load()
    syms = [i["symbol"] for i in d["items"]]
    quotes = rich_quotes(syms) if (config.CHAI_SOURCE == "schwab" and syms) else {}
    g_dip = d["rule"].get("dip_pct", 15.0)
    g_day = d["rule"].get("day_pct", 5.0)
    out = []
    for i in d["items"]:
        q = quotes.get(i["symbol"]) or {}
        price, high52, chg = q.get("price"), q.get("high52"), q.get("chg_pct")
        off_high = round((high52 - price) / high52 * 100, 1) if (price and high52) else None
        thresh = i.get("alert_pct", g_dip)
        high_alert = off_high is not None and off_high >= thresh
        day_alert = chg is not None and chg <= -g_day       # a drop of >= g_day % today
        reasons = []
        if day_alert:
            reasons.append(f"{round(chg, 1)}% today")
        if high_alert:
            reasons.append(f"{off_high}% off high")
        rng = None
        if price and high52 and q.get("low52") and high52 > q["low52"]:
            rng = round((price - q["low52"]) / (high52 - q["low52"]) * 100, 0)  # % up the 52wk range
        out.append({**i, "price": price, "chg_pct": chg,
                    "chg": q.get("chg"), "high52": high52, "low52": q.get("low52"),
                    "off_high": off_high, "range_pos": rng, "alert_pct": thresh,
                    "alert": high_alert or day_alert, "high_alert": high_alert,
                    "day_alert": day_alert, "alert_reason": " · ".join(reasons)})
    return {"status": "ok", "rule": d["rule"], "items": out,
            "alerting": sum(1 for x in out if x["alert"]),
            "day_alerting": sum(1 for x in out if x["day_alert"]),
            "high_alerting": sum(1 for x in out if x["high_alert"])}


def news(symbol: str, force: bool = False) -> dict:
    """Recent Finnhub company news for one ticker (cached ~30 min)."""
    sym = (symbol or "").strip().upper()
    now = time.time()
    hit = _NEWS_CACHE.get(sym)
    if hit and not force and now - hit[0] < _NEWS_TTL:
        return hit[1]
    if not config.FINNHUB_API_KEY:
        return {"symbol": sym, "news": [], "detail": "no_finnhub_key"}
    import datetime
    import httpx
    to = datetime.date.today()
    frm = to - datetime.timedelta(days=10)
    try:
        r = httpx.get("https://finnhub.io/api/v1/company-news",
                      params={"symbol": sym, "from": frm.isoformat(), "to": to.isoformat(),
                              "token": config.FINNHUB_API_KEY}, timeout=15)
        items = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
    except Exception:
        items = []
    items = sorted(items, key=lambda x: x.get("datetime", 0), reverse=True)[:8]
    out = {"symbol": sym, "news": [{"headline": n.get("headline"), "source": n.get("source"),
                                    "url": n.get("url"), "ts": n.get("datetime"),
                                    "summary": (n.get("summary") or "")[:220]} for n in items]}
    _NEWS_CACHE[sym] = (now, out)
    return out
