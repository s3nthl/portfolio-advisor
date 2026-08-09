"""Backtest metrics + data loaders — pure/light (NO matplotlib), so both the API
and the report can import them. harness.py adds only the figure rendering.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from recession import config
from recession.ingest import fred_client
from recession.recessions import ENDOGENOUS
from recession.registry import CORE_25
from recession.scoring import composite as comp
from recession.scoring.build_signals import build_frame
from recession.scoring.engine import to_monthly
from recession.store import db

WARN = config.WARN_CROSS
LOOKBACK = 36
FP_HORIZON = 18
DD_WINDOW = 12
DD_BARS = (10, 15, 20)
DD_RISK_BAR = 10


def _lag_months(s) -> int:
    return 0 if s.native_freq in ("daily", "weekly") else max(0, round(s.lag_days / 30))


def load_modes() -> dict[str, pd.DataFrame]:
    frame = build_frame()
    revised = comp.composite(frame, CORE_25)
    pit_frame = frame.copy()
    for s in CORE_25:
        if s.id in pit_frame.columns and _lag_months(s):
            pit_frame[s.id] = pit_frame[s.id].shift(_lag_months(s))
    pit = comp.composite(pit_frame, CORE_25)
    return {"revised": revised, "pit": pit, "_frame": frame}


def usrec_monthly(index: pd.DatetimeIndex) -> pd.Series:
    raw = fred_client.get_observations("USREC", start=config.BACKTEST_START)
    m = to_monthly(raw.set_index("obs_date")["value"]).reindex(index).fillna(0)
    return (m > 0).astype(int)


def recession_spans(usrec: pd.Series) -> list[dict]:
    """Contiguous NBER recession spans from USREC, for chart shading."""
    spans, start = [], None
    v = usrec.astype(bool).to_numpy(); idx = usrec.index
    for i in range(len(v)):
        if v[i] and start is None:
            start = idx[i]
        if (not v[i] or i == len(v) - 1) and start is not None:
            spans.append({"start": str(start.date()), "end": str(idx[i].date())})
            start = None
    return spans


def forward_drawdown(index: pd.DatetimeIndex) -> pd.Series:
    with db.connect() as c:
        sp = to_monthly(db.read_latest(c, "^GSPC").set_index("obs_date")["value"])
    sp = sp.reindex(index).ffill()
    fdd = pd.Series(index=index, dtype="float64")
    for i in range(len(index)):
        p0 = sp.iloc[i]
        win = sp.iloc[i + 1:i + 1 + DD_WINDOW]
        fdd.iloc[i] = (win.min() / p0 - 1) * 100 if len(win) and not pd.isna(p0) else np.nan
    return fdd


def lead_times(composite: pd.Series) -> list[dict]:
    out = []
    for r in ENDOGENOUS:
        peak = pd.Period(r["peak"], "M").to_timestamp("M")
        win = composite[(composite.index > peak - pd.DateOffset(months=LOOKBACK)) &
                        (composite.index <= peak)]
        crossed = win[win >= WARN]
        if len(crossed):
            out.append({"name": r["name"], "lead": round((peak - crossed.index[0]).days / 30.44), "hit": True})
        else:
            out.append({"name": r["name"], "lead": None, "hit": False})
    return out


def _auc(scores: np.ndarray, labels: np.ndarray) -> float:
    m = ~np.isnan(scores)
    scores, labels = scores[m], labels[m]
    pos, neg = (labels == 1).sum(), (labels == 0).sum()
    if pos == 0 or neg == 0:
        return float("nan")
    ranks = pd.Series(scores).rank().to_numpy()
    return (ranks[labels == 1].sum() - pos * (pos + 1) / 2) / (pos * neg)


def signal_auc(frame: pd.DataFrame, usrec: pd.Series, horizons=(6, 12, 18)) -> pd.DataFrame:
    rows = []
    for s in CORE_25:
        if s.id not in frame.columns:
            continue
        col = frame[s.id].reindex(usrec.index)
        row = {"series": s.id, "section": s.section}
        for h in horizons:
            tgt = pd.Series(0, index=usrec.index)
            for i in range(len(usrec.index)):
                tgt.iloc[i] = 1 if usrec.iloc[i + 1:i + 1 + h].sum() > 0 else 0
            row[f"auc{h}"] = _auc(col.to_numpy(), tgt.to_numpy())
        rows.append(row)
    return pd.DataFrame(rows).sort_values("auc12", ascending=False)


def drawdown_scorecard(composite: pd.Series, fdd: pd.Series) -> list[dict]:
    ok = composite.notna() & fdd.notna()
    rows = []
    for bar in DD_BARS:
        hit = (fdd <= -bar).astype(int)
        base = hit[ok].mean() * 100
        auc = _auc(composite[ok].to_numpy(), hit[ok].to_numpy())
        sig = composite >= WARN
        prec = hit[sig & ok].mean() * 100 if (sig & ok).any() else float("nan")
        rows.append({"bar": bar, "base": round(base, 1), "prec": round(prec, 1),
                     "auc": round(auc, 3), "lift": round(prec / base, 1) if base else None})
    return rows


def false_positives(composite: pd.Series, usrec: pd.Series, fdd: pd.Series) -> list[dict]:
    c = composite.dropna()
    rising = c[(c >= WARN) & (c.shift(1) < WARN)]
    out = []
    for dt in rising.index:
        fwd = usrec[(usrec.index > dt) & (usrec.index <= dt + pd.DateOffset(months=FP_HORIZON))]
        rec = bool(fwd.sum() > 0)
        dd = float(fdd.get(dt, np.nan))
        risk_useful = (not np.isnan(dd)) and dd <= -DD_RISK_BAR
        out.append({"date": str(dt.date()), "recession": rec,
                    "dd": None if np.isnan(dd) else round(dd, 1),
                    "risk_useful": risk_useful, "genuine": not rec and not risk_useful})
    return out


def weight_envelope(frame: pd.DataFrame) -> pd.DataFrame:
    base = comp.composite(frame, CORE_25)["composite"]
    curves = [base]
    for sec in config.SECTION_WEIGHTS:
        for factor in (0.5, 1.5):
            w = dict(config.SECTION_WEIGHTS); w[sec] = w[sec] * factor
            curves.append(comp.composite(frame, CORE_25, w)["composite"])
    M = pd.concat(curves, axis=1)
    return pd.DataFrame({"base": base, "lo": M.min(axis=1), "hi": M.max(axis=1)})


def recession_verdict(leads, fp_rows) -> tuple[str, str]:
    hits = [l for l in leads if l["hit"]]
    n = len(hits)
    good = [l["lead"] for l in hits if l["lead"] is not None]
    med = float(np.median(good)) if good else float("nan")
    rec_fp = [f for f in fp_rows if not f["recession"]]
    if n >= 5 and 4 <= med <= 15 and len(rec_fp) <= 3:
        return "GO", f"{n}/7 led · median {med:.0f}mo · {len(rec_fp)} FPs"
    if n >= 5:
        return "TUNE", f"{n}/7 led · median {med:.0f}mo · {len(rec_fp)} FPs"
    if n >= 3:
        return "MARGINAL", f"{n}/7 led · median {med:.0f}mo"
    return "NO-GO", f"only {n}/7 led"


def risk_verdict(dd_scorecard, fp_rows) -> tuple[str, str]:
    auc15 = next((r["auc"] for r in dd_scorecard if r["bar"] == 15), float("nan"))
    genuine = [f for f in fp_rows if f["genuine"]]
    useful = sum(1 for f in fp_rows if f["risk_useful"] or f["recession"])
    if auc15 >= 0.65 and len(genuine) <= 3:
        return "GO", f"drawdown AUC(15%) {auc15:.2f} · {useful}/{len(fp_rows)} crossings risk-useful · {len(genuine)} genuine FPs"
    if auc15 >= 0.60:
        return "MARGINAL", f"drawdown AUC(15%) {auc15:.2f} · {len(genuine)} genuine FPs"
    return "NO-GO", f"drawdown AUC(15%) {auc15:.2f}"
