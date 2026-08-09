"""Scorer unit tests — the non-negotiables (spec §10): no lookahead, COVID
excluded, coverage reduces the denominator, direction handling."""
from __future__ import annotations

import numpy as np
import pandas as pd

from recession.registry import Series
from recession.scoring import composite as comp
from recession.scoring.engine import (StressCfg, compute_stress,
                                       expanding_percentile, to_monthly, transform)

MIDX = pd.date_range("2000-01-31", periods=60, freq="ME")


def test_expanding_percentile_no_lookahead():
    # a spike at t must not change earlier percentiles; ref is strictly prior
    x = pd.Series(np.arange(60.0), index=MIDX)
    p = expanding_percentile(x, min_hist=5)
    x2 = x.copy(); x2.iloc[-1] = 1e9          # future spike
    p2 = expanding_percentile(x2, min_hist=5)
    assert p.iloc[:-1].equals(p2.iloc[:-1])   # earlier values unchanged
    # a monotonically rising series is always at/near the top of its prior history
    assert p.iloc[-1] == 1.0


def test_min_hist_gates_early_output():
    x = pd.Series(np.arange(60.0), index=MIDX)
    p = expanding_percentile(x, min_hist=24)
    assert p.iloc[:24].isna().all() and p.iloc[24:].notna().all()


def test_covid_excluded_from_reference():
    # a huge COVID spike must not inflate the reference distribution afterwards
    idx = pd.date_range("2019-06-30", periods=24, freq="ME")
    x = pd.Series(10.0, index=idx)
    x.loc["2020-03-31":"2020-12-31"] = 500.0          # COVID spike
    x.iloc[-1] = 12.0                                  # mildly elevated, post-COVID
    from recession.scoring.engine import _covid_mask
    excl = _covid_mask(x.index)
    p = expanding_percentile(x, excl, min_hist=3)
    # 12 vs a reference of ~10s (COVID 500s excluded) → top percentile, not tiny
    assert p.iloc[-1] >= 0.9


def test_direction_flips_stress():
    up = pd.Series(np.arange(1, 61.0), index=MIDX)     # steadily rising
    hi = compute_stress(up, StressCfg("level", "higher_is_worse", 1, 0, min_hist=5))
    lo = compute_stress(up, StressCfg("level", "lower_is_worse", 1, 0, min_hist=5))
    assert hi.iloc[-1] > 80 and lo.iloc[-1] < 20        # same data, inverted verdict


def test_transform_drawdown_no_lookahead():
    x = pd.Series([100, 110, 120, 90, 95], index=pd.date_range("2020-01-31", periods=5, freq="ME"))
    dd = transform(x, "drawdown_from_ath")
    assert dd.iloc[2] == 0.0                            # new ATH → 0 drawdown
    assert round(dd.iloc[3], 1) == 25.0                 # 120→90 = 25% drawdown


def test_composite_coverage_weighted_not_zero_filled():
    idx = MIDX[:3]
    a = Series("A", "fred", "labor", "x", "A", "level", "higher_is_worse", 1.0)
    b = Series("B", "fred", "credit", "x", "B", "level", "higher_is_worse", 1.0)
    frame = pd.DataFrame({"A": [80.0, 80.0, 80.0], "B": [np.nan, np.nan, 20.0]}, index=idx)
    out = comp.composite(frame, [a, b], {"labor": 50, "credit": 50})
    # month 0: only A present -> composite == 80 (B not treated as 0), coverage 50%
    assert round(out["composite"].iloc[0]) == 80 and round(out["coverage_pct"].iloc[0]) == 50
    # month 2: both present, equal section weights -> mean(80,20)=50, coverage 100%
    assert round(out["composite"].iloc[2]) == 50 and round(out["coverage_pct"].iloc[2]) == 100


def test_stress_bounded_0_100():
    x = pd.Series(np.random.default_rng(0).normal(size=120).cumsum() + 50,
                  index=pd.date_range("2000-01-31", periods=120, freq="ME"))
    s = compute_stress(x, StressCfg("level", "higher_is_worse", min_hist=12)).dropna()
    assert s.min() >= 0 and s.max() <= 100
