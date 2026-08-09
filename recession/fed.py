"""Fed path & policy regime — a free, honest proxy for CME FedWatch.

CME FedWatch's per-meeting probabilities are proprietary and computed from 30-Day
Fed Funds futures (no free/legal API). Instead we read the market's rate
expectation straight from the Treasury curve: a T-bill/note yield ≈ the expected
average fed funds over its life, so (EFFR − yield) prices the net cuts by that
horizon. Purely FRED, purely free. This is market-IMPLIED, not CME's odds.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

from recession.ingest import fred_client

# FOMC decision days (second meeting day). 2026 published; early-2027 tentative.
FOMC_DATES = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28",
]

_HORIZONS = [(3, "3 mo", "DGS3MO"), (6, "6 mo", "DGS6MO"), (12, "1 yr", "DGS1"), (24, "2 yr", "DGS2")]
_CACHE: dict = {}
_TTL = 6 * 3600


def _latest(series_id: str, start: str) -> float | None:
    try:
        df = fred_client.get_observations(series_id, start=start).dropna()
        return round(float(df["value"].iloc[-1]), 2) if len(df) else None
    except Exception:
        return None


def _interp(pts: list[tuple[float, float]], t: float) -> float:
    if t <= pts[0][0]:
        return pts[0][1]
    for i in range(1, len(pts)):
        if t <= pts[i][0]:
            (t0, c0), (t1, c1) = pts[i - 1], pts[i]
            return round(c0 + (c1 - c0) * (t - t0) / (t1 - t0), 2)
    return pts[-1][1]


def compute(force: bool = False) -> dict:
    now = time.time()
    if not force and _CACHE.get("t") and now - _CACHE["t"] < _TTL:
        return _CACHE["v"]

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
        meetings.append({"date": d, "months_out": round(mo, 1),
                         "cum_cuts": cum, "inc": round(cum - prev, 2),
                         "tentative": dd.year >= 2027})
        prev = cum
        if len(meetings) >= 8:
            break

    out = {"status": "ok", "as_of": date.today().isoformat(), "effr": effr,
           "upper": upper, "lower": lower, "regime": regime, "color": color,
           "cuts_1y": c1y, "path": path, "meetings": meetings}
    _CACHE.update(t=now, v=out)
    return out
