"""Finnhub enrichment: live sector (industry) + beta per underlying, cached.

Read-only external calls to finnhub.io. Results are cached to a local JSON file
(sector/beta don't change intraday), so only uncached/stale symbols are fetched —
keeping on-demand refreshes fast and within the free-tier rate limit. If no API
key is set or a call fails, it degrades to whatever is cached (and the engine
falls back to the static reference).
"""
from __future__ import annotations

import json
import time

import config

_BASE = "https://finnhub.io/api/v1"


def _load_cache() -> dict:
    try:
        return json.loads(config.FINNHUB_CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        config.FINNHUB_CACHE_PATH.write_text(json.dumps(cache, indent=0))
    except Exception:
        pass


def enrich(symbols, *, force: bool = False) -> dict[str, dict]:
    """Return {symbol: {"sector":str|None, "beta":float|None}} for `symbols`.

    Uses the cache first; fetches only symbols missing or older than
    FINNHUB_CACHE_DAYS. Never raises — failures leave that symbol unenriched.
    """
    cache = _load_cache()
    symbols = sorted({s.upper() for s in symbols})
    key = config.FINNHUB_API_KEY
    now = time.time()
    max_age = config.FINNHUB_CACHE_DAYS * 86400

    to_fetch = []
    for s in symbols:
        c = cache.get(s)
        if force or not c or (now - c.get("fetched_at", 0) > max_age):
            to_fetch.append(s)

    if to_fetch and key:
        import httpx
        with httpx.Client(timeout=8.0) as client:
            for s in to_fetch:
                sector = beta = None
                try:
                    p = client.get(f"{_BASE}/stock/profile2",
                                   params={"symbol": s, "token": key})
                    if p.status_code == 200:
                        sector = (p.json() or {}).get("finnhubIndustry") or None
                    m = client.get(f"{_BASE}/stock/metric",
                                   params={"symbol": s, "metric": "all", "token": key})
                    if m.status_code == 200:
                        beta = ((m.json() or {}).get("metric") or {}).get("beta")
                except Exception:
                    pass  # leave unenriched; engine falls back to static
                cache[s] = {"sector": sector, "beta": beta, "fetched_at": now}
        _save_cache(cache)

    return {s: {"sector": cache.get(s, {}).get("sector"),
                "beta": cache.get(s, {}).get("beta")} for s in symbols}
