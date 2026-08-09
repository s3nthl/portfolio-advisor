"""S2 driver — read latest observations, score every series, build the composite,
write signals.csv, and print the 12 months before each recession peak for eyeballing.

    python -m recession.scoring.build_signals
"""
from __future__ import annotations

import pandas as pd

from recession import config
from recession.recessions import RECESSIONS
from recession.registry import CORE_25
from recession.scoring import composite as comp
from recession.scoring.engine import StressCfg, compute_stress
from recession.store import db


def cfg_for(s) -> StressCfg:
    wl, wv = config.SECTION_BLEND.get(s.section, (config.W_LEVEL_DEFAULT, config.W_VELOCITY_DEFAULT))
    return StressCfg(transform=s.transform, direction=s.direction, w_level=wl, w_velocity=wv)


def build_frame() -> pd.DataFrame:
    stress = {}
    with db.connect() as conn:
        for s in CORE_25:
            raw = db.read_latest(conn, s.id)
            if raw.empty:
                continue
            x = raw.set_index("obs_date")["value"]
            stress[s.id] = compute_stress(x, cfg_for(s))
    frame = pd.DataFrame(stress).sort_index()
    frame = frame[frame.index >= pd.Timestamp(config.BACKTEST_START)]
    return frame


def build_signals() -> pd.DataFrame:
    frame = build_frame()
    c = comp.composite(frame, CORE_25)
    out = frame.join(c)
    out.insert(0, "band", out["composite"].map(comp.band))
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.REPORT_DIR / "signals.csv"
    out.round(1).to_csv(path)
    print(f"signals.csv → {path}  ({len(out)} months × {out.shape[1]} cols)")
    return out


def _preview(out: pd.DataFrame) -> None:
    for r in RECESSIONS:
        peak = pd.Period(r["peak"], "M").to_timestamp("M")
        window = out[(out.index <= peak)].tail(12)
        if window.empty:
            continue
        comp_path = window["composite"].round(0).astype("Int64").tolist()
        cov = window["coverage_pct"].iloc[-1]
        tag = " (exogenous)" if r.get("exogenous") else ""
        print(f"\n{r['name']}{tag} — peak {r['peak']} · composite over prior 12mo "
              f"(coverage {cov:.0f}%):\n  {comp_path}")


if __name__ == "__main__":
    _preview(build_signals())
