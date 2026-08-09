"""Recession Monitor API — thin layer over the DB + pure scorer (scorer stays
pure; this only reads/serves). Mounted into the myport FastAPI app under
/api/recession. Results are cached in-process (observations.db is static between
ingests); pass ?refresh=1 to recompute.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from recession import config
from recession.backtest import metrics as M
from recession.registry import CORE_25, metric_info, section_info
from recession.scoring.composite import band
from recession.store import db

router = APIRouter(prefix="/api/recession")
_C: dict = {}


def _compute() -> None:
    modes = M.load_modes()
    frame = modes["_frame"]
    idx = modes["revised"].index
    usrec = M.usrec_monthly(idx)
    fdd = M.forward_drawdown(idx)
    pit = modes["pit"]
    comp = pit["composite"]
    fp = M.false_positives(comp, usrec, fdd)
    dd = M.drawdown_scorecard(comp, fdd)
    from recession.scoring.engine import to_monthly
    raw = {}
    with db.connect() as c:
        for s in CORE_25:
            d = db.read_latest(c, s.id)
            if not d.empty:
                raw[s.id] = (round(float(d["value"].iloc[-1]), 2), str(d["obs_date"].iloc[-1].date()))
        gspc = db.read_latest(c, "^GSPC")
    spx = to_monthly(gspc.set_index("obs_date")["value"]) if not gspc.empty else None
    _C.update(pit=pit, revised=modes["revised"], comp=comp, frame=frame, usrec=usrec, fdd=fdd,
              raw=raw, spx=spx, lead=M.lead_times(comp), fp=fp, dd=dd,
              rec_v=M.recession_verdict(M.lead_times(comp), fp),
              risk_v=M.risk_verdict(dd, fp),
              spans=M.recession_spans(usrec))


def _ensure(refresh: bool = False) -> None:
    if refresh or "comp" not in _C:
        _C.clear() if refresh else None
        _compute()


def _at(series: pd.Series, dt) -> float | None:
    v = series.get(dt)
    return None if v is None or pd.isna(v) else round(float(v), 1)


@router.get("/meter")
def meter(refresh: bool = False) -> JSONResponse:
    if not config.FRED_API_KEY:
        return JSONResponse({"status": "no_key", "detail": "FRED_API_KEY missing from .env"})
    try:
        _ensure(refresh)
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": "compute_failed", "detail": str(exc)})
    # LIVE reading: one coherent snapshot (latest value per series), same method
    # as /full so the in-wheel tab and the standalone macro page always agree.
    import pandas as pd
    frame = _C["frame"]
    cur = frame.index.max()
    snap = _snapshot(frame, cur, config.SECTION_WEIGHTS)
    if snap["composite"] is None:
        return JSONResponse({"status": "no_data"})
    score = round(snap["composite"], 1)
    s1 = _snapshot(frame, cur - pd.DateOffset(months=1), config.SECTION_WEIGHTS)
    s3 = _snapshot(frame, cur - pd.DateOffset(months=3), config.SECTION_WEIGHTS)
    secset = {x.section for x in CORE_25}
    sections = sorted(
        ({"name": s, "score": round(snap["sections"][s], 1) if s in snap["sections"] else None,
          "weight": config.SECTION_WEIGHTS.get(s, 0)}
         for s in config.SECTION_WEIGHTS if s in secset),
        key=lambda x: -x["weight"])
    risk_v, risk_t = _C["risk_v"]
    dd = {r["bar"]: r for r in _C["dd"]}
    return JSONResponse({
        "status": "ok", "as_of": str(cur.date()), "score": score, "band": band(score),
        "trigger": config.WARN_CROSS,
        "delta_1m": (None if s1["composite"] is None else round(score - s1["composite"], 1)),
        "delta_3m": (None if s3["composite"] is None else round(score - s3["composite"], 1)),
        "coverage_pct": snap["coverage"],
        "sections": sections,
        "risk": {"verdict": risk_v, "detail": risk_t,
                 "dd10": dd.get(10), "dd15": dd.get(15), "dd20": dd.get(20)},
    })


@router.get("/history")
def history(mode: str = "pit", refresh: bool = False) -> JSONResponse:
    try:
        _ensure(refresh)
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": "compute_failed", "detail": str(exc)})
    series = _C["pit"]["composite"] if mode != "revised" else _C["comp"]
    s = series.dropna()
    points = [{"d": str(d.date()), "s": round(float(v), 1)} for d, v in s.items()]
    return JSONResponse({"status": "ok", "trigger": config.WARN_CROSS,
                         "points": points, "recessions": _C["spans"]})


@router.get("/backtest")
def backtest(refresh: bool = False) -> JSONResponse:
    try:
        _ensure(refresh)
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": "compute_failed", "detail": str(exc)})
    rec_v, rec_t = _C["rec_v"]
    risk_v, risk_t = _C["risk_v"]
    return JSONResponse({
        "status": "ok",
        "recession_verdict": {"verdict": rec_v, "detail": rec_t},
        "risk_verdict": {"verdict": risk_v, "detail": risk_t},
        "drawdown_scorecard": _C["dd"],
        "lead_times": _C["lead"],
        "crossings": _C["fp"],
    })


def _last(series) -> tuple:
    s = series.dropna()
    return (round(float(s.iloc[-1]), 1), str(s.index[-1].date())) if len(s) else (None, None)


def _snapshot(frame, as_of, weights, max_stale_m: int = 6) -> dict:
    """One coherent 'where are we now' reading: each series at its latest value
    known by `as_of` (carried forward up to max_stale_m months), with sections and
    composite aggregated from exactly those — so hero = sections = member chips."""
    import pandas as pd
    ms, fresh = {}, 0
    for s in CORE_25:
        if s.id not in frame.columns:
            continue
        col = frame[s.id].dropna()
        col = col[col.index <= as_of]
        if len(col) and (as_of - col.index[-1]).days <= max_stale_m * 31:
            ms[s.id] = float(col.iloc[-1])
            if (as_of - col.index[-1]).days <= 45:
                fresh += 1
    secs: dict[str, list] = {}
    for s in CORE_25:
        if s.id in ms:
            secs.setdefault(s.section, []).append((ms[s.id], s.weight))
    sec_score = {n: sum(v * w for v, w in lst) / sum(w for _, w in lst) for n, lst in secs.items()}
    num = sum(sec_score[n] * weights.get(n, 0) for n in sec_score)
    den = sum(weights.get(n, 0) for n in sec_score)
    total = len([s for s in CORE_25 if s.id in frame.columns])
    return {"composite": (num / den if den else None), "sections": sec_score,
            "members": ms, "coverage": round(fresh / total * 100, 0) if total else 0}


@router.get("/fedpath")
def fedpath(refresh: bool = False) -> JSONResponse:
    """Market-implied Fed path + policy regime from the Treasury curve (free proxy
    for CME FedWatch — not CME's futures-based probabilities)."""
    if not config.FRED_API_KEY:
        return JSONResponse({"status": "no_key"})
    try:
        from recession import fed
        return JSONResponse(fed.compute(force=refresh))
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": "fed_failed", "detail": str(exc)})


@router.get("/full")
def full(refresh: bool = False) -> JSONResponse:
    """Everything the macro page needs: meter + per-section member metrics with
    values, current stress, sparklines and explanations, plus section correlations."""
    if not config.FRED_API_KEY:
        return JSONResponse({"status": "no_key"})
    try:
        _ensure(refresh)
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": "compute_failed", "detail": str(exc)})
    import pandas as pd
    rev, frame, raw = _C["revised"], _C["frame"], _C["raw"]
    cur = frame.index.max()
    snap = _snapshot(frame, cur, config.SECTION_WEIGHTS)
    if snap["composite"] is None:
        return JSONResponse({"status": "no_data"})
    score = round(snap["composite"], 1)
    s1 = _snapshot(frame, cur - pd.DateOffset(months=1), config.SECTION_WEIGHTS)
    s3 = _snapshot(frame, cur - pd.DateOffset(months=3), config.SECTION_WEIGHTS)

    # group registry by section, aggregate from the SAME snapshot
    by_sec: dict[str, list] = {}
    for s in CORE_25:
        if s.id in frame.columns:
            by_sec.setdefault(s.section, []).append(s)

    sections = []
    for name, members in by_sec.items():
        info = section_info(name)
        mlist = []
        for m in members:
            stress_now = round(snap["members"][m.id], 1) if m.id in snap["members"] else None
            val, vdate = raw.get(m.id, (None, None))
            spark = [{"d": str(d.date()), "s": round(float(v), 1)}
                     for d, v in frame[m.id].dropna().tail(60).items()]
            mlist.append({"id": m.id, "name": m.name, "subsector": m.subsector,
                          "value": val, "value_date": vdate, "stress": stress_now,
                          "transform": m.transform, "direction": m.direction,
                          "explain": metric_info(m.id), "spark": spark})
        mlist.sort(key=lambda x: (x["stress"] is None, -(x["stress"] or 0)))
        sec_score = snap["sections"].get(name)
        sections.append({"name": name, "label": info["label"],
                         "score": round(sec_score, 1) if sec_score is not None else None,
                         "weight": config.SECTION_WEIGHTS.get(name, 0),
                         "what": info["what"], "why": info["why"], "members": mlist})
    sections.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))

    # section-level correlations over the last 15y
    seccols = [c for c in rev.columns if c.startswith("sec_")]
    corr = rev[seccols].tail(180).corr().round(2)
    corr_labels = [section_info(c[4:])["label"] for c in seccols]

    risk_v, risk_t = _C["risk_v"]
    dd = {r["bar"]: r for r in _C["dd"]}
    return JSONResponse({
        "status": "ok", "as_of": str(cur.date()), "score": score, "band": band(score),
        "trigger": config.WARN_CROSS,
        "delta_1m": (None if s1["composite"] is None else round(score - s1["composite"], 1)),
        "delta_3m": (None if s3["composite"] is None else round(score - s3["composite"], 1)),
        "coverage_pct": snap["coverage"],
        "sections": sections,
        "correlations": {"labels": corr_labels, "matrix": corr.values.tolist()},
        "history": {"trigger": config.WARN_CROSS, "recessions": _C["spans"],
                    "points": [{"d": str(d.date()), "s": round(float(v), 1)}
                               for d, v in _C["comp"].dropna().items()],
                    "spx": ([{"d": str(d.date()), "v": round(float(v), 1)}
                             for d, v in _C["spx"].items()
                             if d >= _C["comp"].dropna().index[0]]
                            if _C.get("spx") is not None else [])},
        "risk": {"detail": risk_t, "dd10": dd.get(10), "dd15": dd.get(15), "dd20": dd.get(20)},
        "crossings": _C["fp"],
    })
