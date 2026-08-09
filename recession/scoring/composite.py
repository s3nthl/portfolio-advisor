"""Composite meter (spec §3.2). Coverage-weighted: a missing series REDUCES the
denominator — never zero-filled. Section weights renormalize over whichever
sections actually have data on each date. Pure over a stress DataFrame.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from recession import config


def band(score: float) -> str:
    if score is None or (isinstance(score, float) and np.isnan(score)):
        return "—"
    for lo, hi, name in config.BANDS:
        if lo <= score < hi:
            return name
    return config.BANDS[-1][2]


def _weighted(frame: pd.DataFrame, cols: list[str], w: np.ndarray) -> np.ndarray:
    sub = frame[cols]
    mask = sub.notna().to_numpy()
    vals = np.nan_to_num(sub.to_numpy())
    wsum = (mask * w).sum(axis=1)
    ssum = (vals * w).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(wsum > 0, ssum / wsum, np.nan)


def composite(frame: pd.DataFrame, series_list,
              section_weights: dict | None = None) -> pd.DataFrame:
    """frame: month × series_id stress. Returns month-indexed DataFrame with
    `composite`, `coverage_pct`, and one `sec_<name>` column per section."""
    section_weights = section_weights or config.SECTION_WEIGHTS
    idx = frame.index
    present_series = [s for s in series_list if s.id in frame.columns]

    by_sec: dict[str, list] = {}
    for s in present_series:
        by_sec.setdefault(s.section, []).append(s)

    sec_scores = {}
    for sec, members in by_sec.items():
        cols = [m.id for m in members]
        w = np.array([m.weight for m in members], dtype=float)
        sec_scores[sec] = _weighted(frame, cols, w)

    S = pd.DataFrame(sec_scores, index=idx)                 # month × section
    W = np.array([section_weights.get(sec, 0.0) for sec in S.columns], dtype=float)
    comp = _weighted(S.rename(columns={c: c for c in S.columns}),
                     list(S.columns), W) if len(S.columns) else np.full(len(idx), np.nan)

    total = max(1, len(present_series))
    coverage = frame[[s.id for s in present_series]].notna().sum(axis=1) / total * 100

    out = pd.DataFrame({"composite": comp, "coverage_pct": coverage.to_numpy()}, index=idx)
    for sec in S.columns:
        out[f"sec_{sec}"] = S[sec].to_numpy()
    return out
