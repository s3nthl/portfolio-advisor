"""NBER recession reference (spec §9). Labels/alignment/exogenous flag; USREC is
the machine-readable shading source. 2020 is exogenous — excluded from averages.
"""
from __future__ import annotations

RECESSIONS = [
    {"name": "1969-70", "peak": "1969-12", "trough": "1970-11", "exogenous": 0},
    {"name": "1973-75", "peak": "1973-11", "trough": "1975-03", "exogenous": 0, "notes": "oil shock / stagflation"},
    {"name": "1980", "peak": "1980-01", "trough": "1980-07", "exogenous": 0, "notes": "Volcker"},
    {"name": "1981-82", "peak": "1981-07", "trough": "1982-11", "exogenous": 0, "notes": "Volcker II"},
    {"name": "1990-91", "peak": "1990-07", "trough": "1991-03", "exogenous": 0},
    {"name": "2001", "peak": "2001-03", "trough": "2001-11", "exogenous": 0},
    {"name": "2007-09", "peak": "2007-12", "trough": "2009-06", "exogenous": 0},
    {"name": "2020", "peak": "2020-02", "trough": "2020-04", "exogenous": 1, "notes": "pandemic — EXCLUDE from averages"},
]

ENDOGENOUS = [r for r in RECESSIONS if not r.get("exogenous")]
