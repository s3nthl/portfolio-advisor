"""Recession Monitor config — weights, bands, normalization params, paths.

Everything tunable lives here (spec §3.2 / §10). Weights are PRIORS from the spec
and are frozen — do not tune them to flatter the backtest.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent          # .../myport/recession
REPO_DIR = BASE_DIR.parent                            # .../myport

# Reuse myport's .env (FRED_API_KEY already lives there). Never override live env.
load_dotenv(REPO_DIR / ".env")
_shared = os.getenv("CHAI_SHARED_ENV")
if _shared:
    load_dotenv(Path(_shared).expanduser(), override=False)

FRED_API_KEY = os.getenv("FRED_API_KEY", "").strip()

DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "observations.db"
REPORT_DIR = BASE_DIR / "reports"

FRED_BASE = "https://api.stlouisfed.org/fred"
BACKTEST_START = "1969-01-01"

# --- normalization (spec §3.1) -------------------------------------------------
# COVID months excluded from ALL reference distributions (still displayed later).
COVID_MONTHS = ("2020-03", "2020-12")   # inclusive range, YYYY-MM
WINSOR = (0.01, 0.99)                    # winsorize before any z
W_LEVEL_DEFAULT = 0.5
W_VELOCITY_DEFAULT = 0.5
# Labor leads via velocity (Sahm rule is a velocity measure) → weight velocity higher.
SECTION_BLEND = {"labor": (0.35, 0.65)}   # section -> (w_level, w_velocity)

# --- composite (spec §3.2) — PRIORS, FROZEN -----------------------------------
SECTION_WEIGHTS = {
    "labor": 18,
    "yield_curve": 15,
    "credit": 15,
    "growth_activity": 12,
    "leading_surveys": 10,
    "equities": 8,
    "housing": 8,
    "inflation": 6,
    "commodities": 4,
    "international": 4,
}

# staleness decay: contribution weight = exp(-days_since_release / LAG_HALFLIFE)
LAG_HALFLIFE_DAYS = 45.0

# display bands (spec §3.2) — display only, not the computation
BANDS = [
    (0, 20, "Expansion"),
    (20, 40, "Mid-cycle"),
    (40, 60, "Late-cycle"),
    (60, 80, "Warning"),
    (80, 100, "Contraction"),
]
WARN_CROSS = 70.0   # analytical crossing level (S4-tuned from 60 → 70: cut false
                    # alarms 6→1 and lifted 15%-drawdown precision 35%→45%; recession
                    # hits fell 5→3, an accepted trade — market-risk is the objective).
