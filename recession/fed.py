"""Fed path & policy regime — a free, honest proxy for CME FedWatch.

CME FedWatch's per-meeting probabilities are proprietary and computed from 30-Day
Fed Funds futures (no free/legal API). Instead we read the market's rate
expectation straight from the Treasury curve: a T-bill/note yield ≈ the expected
average fed funds over its life, so (EFFR − yield) prices the net cuts by that
horizon. Purely FRED, purely free. This is market-IMPLIED, not CME's odds.
"""
from __future__ import annotations

import pickle
import time
from datetime import date, timedelta

import pandas as pd

from recession import config
from recession.ingest import fred_client

# FOMC decision days (second meeting day). 2026 published; early-2027 tentative.
FOMC_DATES = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28",
]

_HORIZONS = [(3, "3 mo", "DGS3MO"), (6, "6 mo", "DGS6MO"), (12, "1 yr", "DGS1"), (24, "2 yr", "DGS2")]
_CACHE: dict = {}
_TTL = 12 * 3600   # Treasury-implied path moves slowly; stale-while-revalidate below
_CACHE_FILE = config.DATA_DIR / "_fedpath_cache.pkl"


def _load_disk() -> None:
    """Seed the in-process cache from disk so a restart doesn't re-hit FRED."""
    if _CACHE.get("v") is not None:
        return
    try:
        if _CACHE_FILE.exists():
            blob = pickle.loads(_CACHE_FILE.read_bytes())
            _CACHE.update(t=blob["t"], v=blob["v"])
    except Exception:
        pass


def _latest(series_id: str, start: str) -> float | None:
    try:
        df = fred_client.get_observations(series_id, start=start).dropna()
        return round(float(df["value"].iloc[-1]), 2) if len(df) else None
    except Exception:
        return None


def _series(series_id: str, start: str) -> pd.Series | None:
    try:
        df = fred_client.get_observations(series_id, start=start).dropna()
        if df.empty:
            return None
        s = df.set_index("obs_date")["value"]
        s.index = pd.to_datetime(s.index)
        return s
    except Exception:
        return None


def _implied_history(start: str) -> list[dict]:
    """Weekly market-implied 12-mo policy expectation over time: (EFFR − 1y yield)
    / 25bp = net cuts(+)/hikes(−) the curve priced at each date. Shows the shift."""
    dff, dgs1 = _series("DFF", start), _series("DGS1", start)
    if dff is None or dgs1 is None:
        return []
    idx = dff.index.union(dgs1.index)
    implied = (dff.reindex(idx).ffill() - dgs1.reindex(idx).ffill()) / 0.25
    wk = implied.resample("W").last().dropna()
    return [{"d": str(d.date()), "v": round(float(v), 2)} for d, v in wk.items()]


def _interp(pts: list[tuple[float, float]], t: float) -> float:
    if t <= pts[0][0]:
        return pts[0][1]
    for i in range(1, len(pts)):
        if t <= pts[i][0]:
            (t0, c0), (t1, c1) = pts[i - 1], pts[i]
            return round(c0 + (c1 - c0) * (t - t0) / (t1 - t0), 2)
    return pts[-1][1]


_REFRESHING = {"on": False}


def compute(force: bool = False) -> dict:
    """Stale-while-revalidate: return the cached path instantly and, if it's
    stale, refresh it in the background — so the ~9 sequential FRED calls (~30s
    cold) never block a page load after the first-ever compute."""
    now = time.time()
    if not force:
        _load_disk()
        cached = _CACHE.get("v")
        age = now - (_CACHE.get("t") or 0)
        if cached is not None:
            if age >= _TTL and not _REFRESHING["on"]:
                _REFRESHING["on"] = True
                import threading
                threading.Thread(target=_do_compute, daemon=True).start()
            return cached
    return _do_compute()


def _do_compute() -> dict:
    try:
        return _compute_impl()
    finally:
        _REFRESHING["on"] = False


def _compute_impl() -> dict:
    now = time.time()
    start = (date.today() - timedelta(days=900)).isoformat()
    effr = _latest("DFF", start)
    upper, lower = _latest("DFEDTARU", start), _latest("DFEDTARL", start)
    if effr is None:
        return {"status": "no_data"}

    yields = {h: _latest(sid, start) for h, _lbl, sid in _HORIZONS}
    cuts = lambda y: round((effr - y) / 0.25, 1) if y is not None else None
    path = [{"months": h, "label": lbl, "rate": yields[h],
             "delta_bps": (round((yields[h] - effr) * 100) if yields[h] is not None else None),
             "cuts": cuts(yields[h])} for h, lbl, _sid in _HORIZONS]

    c1y = cuts(yields[12]) or 0.0
    regime, color = (("DOVISH", "green") if c1y >= 0.5 else
                     ("HAWKISH", "red") if c1y <= -0.5 else ("ON HOLD", "amber"))

    pts = [(0.0, 0.0)] + [(float(h), cuts(yields[h])) for h in (3, 6, 12, 24) if cuts(yields[h]) is not None]
    today = date.today()
    meetings, prev = [], 0.0
    for d in FOMC_DATES:
        dd = date.fromisoformat(d)
        if dd < today:
            continue
        mo = (dd - today).days / 30.44
        cum = _interp(pts, mo)
        inc = cum - prev                          # this meeting's expected move (25bp steps)
        move = int(min(100, round(abs(inc) * 100)))   # P(a 25bp move) — single-step proxy
        if inc > 0.03:
            direction, p_cut, p_hike = "cut", move, 0
        elif inc < -0.03:
            direction, p_cut, p_hike = "hike", 0, move
        else:
            direction, p_cut, p_hike, move = "hold", 0, 0, 0
        meetings.append({"date": d, "months_out": round(mo, 1),
                         "cum_cuts": cum, "inc": round(inc, 2), "dir": direction,
                         "move_pct": move, "p_cut": p_cut, "p_hike": p_hike,
                         "p_hold": 100 - move, "tentative": dd.year >= 2027})
        prev = cum
        if len(meetings) >= 8:
            break

    hist_start = (date.today() - timedelta(days=1825)).isoformat()   # ~5y of history
    out = {"status": "ok", "as_of": date.today().isoformat(), "effr": effr,
           "upper": upper, "lower": lower, "regime": regime, "color": color,
           "cuts_1y": c1y, "path": path, "meetings": meetings,
           "history": _implied_history(hist_start)}
    _CACHE.update(t=now, v=out)
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_bytes(pickle.dumps({"t": now, "v": out}))
    except Exception:
        pass
    return out
