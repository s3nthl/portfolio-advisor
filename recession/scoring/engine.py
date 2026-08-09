"""Per-indicator stress pipeline (spec §3.1) — a PURE function over a pandas
Series. No I/O, no globals, no DB. This is the unit that must be backtestable.

    raw → resample-monthly → transform → expanding-percentile (level + velocity,
    NO lookahead, COVID-excluded) → direction-adjust → stress 0..100
"""
from __future__ import annotations

from bisect import bisect_right, insort
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class StressCfg:
    transform: str
    direction: str                 # higher_is_worse | lower_is_worse
    w_level: float = 0.5
    w_velocity: float = 0.5
    vel_window: int = 3            # months, for the velocity (deterioration) term
    min_hist: int = 24            # min prior reference points before emitting a score


def _covid_mask(idx: pd.DatetimeIndex, covid=("2020-03", "2020-12")) -> np.ndarray:
    lo, hi = pd.Period(covid[0], "M"), pd.Period(covid[1], "M")
    per = idx.to_period("M")
    return (per >= lo) & (per <= hi)


def to_monthly(x: pd.Series) -> pd.Series:
    """Month-end sampling (last obs in month). Daily/weekly → monthly grid."""
    x = x.dropna().sort_index()
    return x.resample("ME").last()


def transform(x: pd.Series, kind: str) -> pd.Series:
    """Apply a registry transform. x is monthly."""
    if kind == "level":
        return x
    if kind == "yoy":
        return x.pct_change(12) * 100
    if kind == "mom":
        return x.pct_change(1) * 100
    if kind == "3m_ann":
        return ((x / x.shift(3)) ** 4 - 1) * 100
    if kind == "6m_chg":
        return x.diff(6)
    if kind == "4wk_ma":                     # already smoothed post-resample
        return x
    if kind == "drawdown_from_ath":          # positive %, deeper = worse (no lookahead)
        return (x.cummax() - x) / x.cummax() * 100
    raise ValueError(f"unknown transform {kind!r}")


def expanding_percentile(x: pd.Series, exclude: np.ndarray | None = None,
                         min_hist: int = 24) -> pd.Series:
    """For each t: percentile of x[t] within {x[s] : s<t, s not excluded}.

    NO lookahead (strictly prior reference). COVID months are excluded from the
    reference set but still receive a score. O(n log n) via a running sorted list.
    """
    vals = x.to_numpy(dtype="float64")
    keep = np.ones(len(x), dtype=bool) if exclude is None else ~exclude
    out = np.full(len(x), np.nan)
    ref: list[float] = []
    for i in range(len(x)):
        xi = vals[i]
        if not np.isnan(xi) and len(ref) >= min_hist:
            out[i] = bisect_right(ref, xi) / len(ref)
        if keep[i] and not np.isnan(xi):
            insort(ref, xi)
    return pd.Series(out, index=x.index)


def compute_stress(x: pd.Series, cfg: StressCfg) -> pd.Series:
    """Monthly stress 0..100 for one series. Pure."""
    m = to_monthly(x)
    t = transform(m, cfg.transform)
    excl = _covid_mask(t.index)
    lvl = expanding_percentile(t, excl, cfg.min_hist)
    vel = expanding_percentile(t.diff(cfg.vel_window), excl, cfg.min_hist)
    s = cfg.w_level * lvl + cfg.w_velocity * vel
    # where velocity is unavailable (early history) fall back to level alone
    s = s.where(vel.notna(), lvl)
    if cfg.direction == "lower_is_worse":
        s = 1 - s
    return (100 * s).clip(0, 100)
