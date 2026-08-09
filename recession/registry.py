"""Series registry — loaded from registry.yaml (pure data). Adding a metric is a
one-block YAML edit; nothing here or in the UI needs to change (S9).

The dataclass + accessors are unchanged so the rest of the module keeps importing
`CORE_25`, `Series`, `section_info`, `metric_info` exactly as before.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_YAML = Path(__file__).resolve().parent / "registry.yaml"


@dataclass(frozen=True)
class Series:
    id: str
    source: str
    section: str
    subsector: str
    name: str
    transform: str
    direction: str
    weight: float = 1.0
    native_freq: str = "monthly"
    lag_days: int = 30
    fallback: str = ""
    short: bool = False
    explain: str = ""
    notes: str = ""


def _load():
    doc = yaml.safe_load(_YAML.read_text())
    sections = doc.get("sections", {})
    series = []
    for r in doc.get("series", []):
        series.append(Series(
            id=r["id"], source=r["source"], section=r["section"],
            subsector=r.get("subsector", ""), name=r["name"],
            transform=r["transform"], direction=r["direction"],
            weight=float(r.get("weight", 1.0)), native_freq=r.get("native_freq", "monthly"),
            lag_days=int(r.get("lag_days", 30)), fallback=r.get("fallback", ""),
            short=bool(r.get("short", False)), explain=r.get("explain", ""),
            notes=r.get("notes", "")))
    return sections, series


SECTIONS, CORE_25 = _load()
FRED_IDS = [s.id for s in CORE_25 if s.source == "fred"]
PRICE_IDS = [s.id for s in CORE_25 if s.source == "price"]


def by_id(sid: str) -> Series | None:
    return next((s for s in CORE_25 if s.id == sid), None)


def section_info(name: str) -> dict:
    s = SECTIONS.get(name)
    if s:
        return {"label": s.get("label", name), "what": s.get("what", ""), "why": s.get("why", "")}
    return {"label": name.replace("_", " ").title(), "what": "", "why": ""}


def metric_info(sid: str) -> str:
    s = by_id(sid)
    return s.explain if s else ""
