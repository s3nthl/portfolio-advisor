"""Fundamental stock screener — a transparent, reproducible quality/value ranking.

Pure functions over Polygon financials + a current price. No opinions baked in
beyond the weights: it ranks on what the numbers say and shows the reasons, so
it is research/ideation, not advice. Scoring is sector-relative (percentile vs
peers) because a 15x P/E or a 25% margin means different things per sector.

Pillars (weighted): Quality 35 · Growth 25 · Valuation 20 · Consistency 20.
Hard disqualifiers (latest net loss / negative equity / thin history) drop a name
from ranking with a stated reason rather than letting a fatal flaw average away.
"""
from __future__ import annotations

WEIGHTS = {"quality": 0.35, "growth": 0.25, "valuation": 0.20, "consistency": 0.20}


def _vals(fin: dict, name: str) -> list[float]:
    m = (fin.get("metrics") or {}).get(name) or {}
    return [x["v"] for x in m.get("data", []) if x.get("v") is not None]


def _latest(fin: dict, name: str):
    vs = _vals(fin, name)
    return vs[-1] if vs else None


def _cagr(vals: list[float], cap_years: int = 5):
    """Compound annual growth over the last `cap_years` points. None if not clean."""
    vs = [v for v in vals if v is not None]
    if len(vs) < 2:
        return None
    vs = vs[-(cap_years + 1):]
    first, last = vs[0], vs[-1]
    yrs = len(vs) - 1
    if first is None or first <= 0 or last is None or last <= 0 or yrs < 1:
        return None
    return (last / first) ** (1 / yrs) - 1


def raw_metrics(fin: dict, price: float | None) -> dict:
    """Extract one ticker's raw fundamental signals from Polygon data + price."""
    basis = fin.get("basis") or []
    latest_eps = None
    latest_rev = None
    latest_shares = None
    for b in reversed(basis):
        if latest_eps is None and b.get("eps") is not None:
            latest_eps = b["eps"]
        if latest_rev is None and b.get("revenue") is not None:
            latest_rev = b["revenue"]
        if latest_shares is None and b.get("shares") is not None:
            latest_shares = b["shares"]

    op_inc, rev = _latest(fin, "Operating Income"), _latest(fin, "Revenue")
    op_margin = (op_inc / rev * 100) if (op_inc is not None and rev) else None
    ocf, ni = _latest(fin, "Cash from Operations"), _latest(fin, "Net Income")
    # cash conversion (OCF/NI); clamp so a near-zero NI can't explode the ratio
    ocf_quality = min(ocf / ni, 3.0) if (ocf is not None and ni and ni > 0) else None

    nm = _vals(fin, "Net Margin")
    margin_trend = (nm[-1] - nm[-min(len(nm), 6)]) if len(nm) >= 2 else None

    ni_series = _vals(fin, "Net Income")
    ni_pos_frac = (sum(1 for v in ni_series if v > 0) / len(ni_series)) if ni_series else None

    pe = (price / latest_eps) if (price and latest_eps and latest_eps > 0) else None
    ps = (price * latest_shares / latest_rev) if (price and latest_shares and latest_rev) else None

    return {
        "roe": _latest(fin, "Return on Equity"),
        "net_margin": _latest(fin, "Net Margin"),
        "gross_margin": _latest(fin, "Gross Margin"),
        "op_margin": op_margin,
        "debt_equity": _latest(fin, "Debt / Equity"),
        "ocf_quality": ocf_quality,
        "rev_cagr": _cagr(_vals(fin, "Revenue")),
        "eps_cagr": _cagr(_vals(fin, "EPS (diluted)")),
        "margin_trend": margin_trend,
        "ni_pos_frac": ni_pos_frac,
        "pe": pe,
        "ps": ps,
        "years": len(basis),
        "latest_ni": ni,
        "latest_equity": _latest(fin, "Shareholder Equity"),
        "price": price,
    }


def _disqualify(m: dict) -> str | None:
    if m.get("years", 0) < 3:
        return "insufficient history (<3y)"
    if m.get("latest_ni") is not None and m["latest_ni"] <= 0:
        return "not currently profitable"
    # NOTE: negative shareholder equity alone is NOT a disqualifier — for mature,
    # highly-profitable names (MCD, SBUX, PM, ABBV…) it's the result of large
    # buybacks, not distress. ROE / Debt-Equity simply read as null for them, so
    # they're scored on margins, growth, valuation and consistency instead.
    return None


# (metric key, higher_is_better, pillar, pretty label, formatter)
_FACTORS = [
    ("roe", True, "quality", "ROE", lambda v: f"{v:.0f}%"),
    ("net_margin", True, "quality", "net margin", lambda v: f"{v:.0f}%"),
    ("gross_margin", True, "quality", "gross margin", lambda v: f"{v:.0f}%"),
    ("op_margin", True, "quality", "op margin", lambda v: f"{v:.0f}%"),
    ("ocf_quality", True, "quality", "cash conversion", lambda v: f"{v:.1f}x"),
    ("debt_equity", False, "quality", "low debt", lambda v: f"D/E {v:.2f}"),
    ("rev_cagr", True, "growth", "revenue growth", lambda v: f"{v*100:.0f}%/yr"),
    ("eps_cagr", True, "growth", "EPS growth", lambda v: f"{v*100:.0f}%/yr"),
    ("margin_trend", True, "growth", "margin trend", lambda v: f"{v:+.0f}pp"),
    ("pe", False, "valuation", "cheap P/E", lambda v: f"P/E {v:.0f}"),
    ("ps", False, "valuation", "cheap P/S", lambda v: f"P/S {v:.1f}"),
    ("ni_pos_frac", True, "consistency", "profit consistency", lambda v: f"{v*100:.0f}% yrs profitable"),
]


def _percentiles(rows: list[dict], key: str, higher_better: bool) -> dict[str, float]:
    """Map symbol -> 0..1 percentile within the group for one metric."""
    have = [(r["symbol"], r["raw"][key]) for r in rows if r["raw"].get(key) is not None]
    if len(have) < 2:
        return {sym: 0.5 for sym, _ in have}
    have.sort(key=lambda x: x[1], reverse=higher_better)  # best first (index 0 -> percentile 1.0)
    out = {}
    n = len(have)
    for i, (sym, _) in enumerate(have):
        out[sym] = round(1 - i / (n - 1), 4)   # best -> 1.0, worst -> 0.0
    return out


def run_screen(data_by_symbol: dict[str, dict]) -> dict:
    """data_by_symbol: {sym: {"fin": <polygon financials>, "price": float, "sector": str}}.

    Returns per-sector rankings + a global list, each scored 0-100 with reasons.
    Percentiles are computed WITHIN sector (peer-relative).
    """
    # build rows with raw metrics
    rows = []
    excluded = []
    for sym, d in data_by_symbol.items():
        raw = raw_metrics(d.get("fin") or {}, d.get("price"))
        rec = {"symbol": sym, "sector": d.get("sector"), "raw": raw, "price": d.get("price")}
        dq = _disqualify(raw)
        if dq:
            excluded.append({"symbol": sym, "sector": d.get("sector"), "reason": dq})
        else:
            rows.append(rec)

    # group by sector and percentile-rank each factor within the sector
    by_sector: dict[str, list[dict]] = {}
    for r in rows:
        by_sector.setdefault(r["sector"], []).append(r)

    for sector, srows in by_sector.items():
        pcts = {}
        for key, hib, _pillar, _lbl, _fmt in _FACTORS:
            pcts[key] = _percentiles(srows, key, hib)
        for r in srows:
            pillar_scores = {p: [] for p in WEIGHTS}
            reasons = []
            for key, hib, pillar, lbl, fmt in _FACTORS:
                p = pcts[key].get(r["symbol"])
                if p is None:
                    continue
                pillar_scores[pillar].append(p)
                v = r["raw"].get(key)
                if p >= 0.8 and v is not None:      # a genuine standout -> a "reason"
                    reasons.append({"label": lbl, "value": fmt(v), "pct": round(p * 100)})
            pillars = {p: (round(sum(vs) / len(vs) * 100) if vs else None)
                       for p, vs in pillar_scores.items()}
            score = sum(WEIGHTS[p] * (pillars[p] if pillars[p] is not None else 50) for p in WEIGHTS)
            r["pillars"] = pillars
            r["score"] = round(score, 1)
            reasons.sort(key=lambda x: -x["pct"])
            r["reasons"] = reasons[:4]

    # assemble output
    sectors_out = []
    for sector, srows in by_sector.items():
        srows.sort(key=lambda r: -r["score"])
        for i, r in enumerate(srows):
            r["sector_rank"] = i + 1
        sectors_out.append({
            "sector": sector,
            "picks": [_slim(r) for r in srows],
            "top": _slim(srows[0]) if srows else None,
        })
    sectors_out.sort(key=lambda s: s["sector"])

    global_ranked = sorted(rows, key=lambda r: -r["score"])
    for i, r in enumerate(global_ranked):
        r["global_rank"] = i + 1

    return {
        "sectors": sectors_out,
        "global_top": [_slim(r) for r in global_ranked[:15]],
        "excluded": excluded,
        "weights": WEIGHTS,
        "scored": len(rows),
    }


def _slim(r: dict) -> dict:
    raw = r["raw"]
    neg_eq = raw.get("latest_equity") is not None and raw["latest_equity"] < 0
    return {
        "symbol": r["symbol"], "sector": r["sector"], "score": r["score"],
        "pillars": r["pillars"], "reasons": r["reasons"],
        "sector_rank": r.get("sector_rank"), "global_rank": r.get("global_rank"),
        "price": r.get("price"), "neg_equity": neg_eq,
        "metrics": {
            "roe": raw.get("roe"), "net_margin": raw.get("net_margin"),
            "gross_margin": raw.get("gross_margin"), "op_margin": raw.get("op_margin"),
            "rev_cagr": raw.get("rev_cagr"), "eps_cagr": raw.get("eps_cagr"),
            "debt_equity": raw.get("debt_equity"), "pe": raw.get("pe"), "ps": raw.get("ps"),
            "ni_pos_frac": raw.get("ni_pos_frac"), "years": raw.get("years"),
        },
    }
