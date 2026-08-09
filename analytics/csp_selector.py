"""Cash-secured-put selector — pure functions over a live PUT chain.

Mirrors the covered-call advisor, but for the *entry* side of the wheel: punch in
a ticker, get put candidates across DTE buckets (2w/3w/4w/5w) at three risk tiers.

Tier = target absolute delta (≈ assignment probability):
  Conservative 0.15 (far OTM, safe) · Moderate 0.20 · Aggressive 0.30 (near money).

Premium collected = the current BID (a realistic sell fill), never the mark.
Only OTM puts (strike below spot) are offered — a standard cash-secured put.
"""
from __future__ import annotations

# (label, target |delta|) — the risk ladder, safe -> aggressive
DELTA_TIERS = [("Conservative", 0.15), ("Moderate", 0.20), ("Aggressive", 0.30)]
NEAR_MONEY_DELTA = 0.30  # at/above this = elevated assignment risk

# DTE buckets for the term-structure toggle (one chain fetch feeds all)
DTE_BUCKETS = [("2w", 14), ("3w", 21), ("4w", 28), ("5w", 35)]
DEFAULT_BUCKET = "4w"


def _pick(label, target_delta, put, spot, dte, contracts):
    prem = put["bid"]
    strike = put["strike"]
    adelta = abs(put["delta"])
    cash = strike * 100 * contracts
    total = round(prem * 100 * contracts, 2)
    static = prem / strike if strike else 0.0            # yield on cash secured / cycle
    ann = static * 365 / dte if dte else 0.0
    breakeven = strike - prem                             # effective buy price if assigned
    cushion = (spot - strike) / spot if spot else 0.0     # OTM distance (room to fall)
    discount = (spot - breakeven) / spot if spot else 0.0  # discount to spot if assigned
    return {
        "label": label, "target_delta": target_delta,
        "strike": strike, "delta": round(adelta, 3), "dte": dte,
        "premium": round(prem, 2), "total_premium": total,
        "cash_secured": round(cash, 2),
        "static_yield_pct": round(static * 100, 2),
        "annual_yield_pct": round(ann * 100, 1),
        "assignment_prob_pct": round(adelta * 100, 1),
        "breakeven": round(breakeven, 2),
        "cushion_pct": round(cushion * 100, 2),          # how far it can fall before ITM
        "discount_pct": round(discount * 100, 2),        # effective discount if assigned
        "open_interest": put.get("oi", 0),
        "near_money": adelta >= NEAR_MONEY_DELTA,
    }


def recommend_csps(chain: dict, target_dte: int = 28, contracts: int = 1,
                   tiers=DELTA_TIERS) -> dict:
    """CSP candidates for one ticker at a target DTE, three risk tiers."""
    spot = chain.get("underlying")
    exps = chain.get("expirations") or []
    if not spot or not exps:
        return {"status": "no_chain", "picks": []}

    exp = min(exps, key=lambda e: abs(e["dte"] - target_dte))
    dte = exp["dte"]
    # standard CSP: OTM puts (strike below spot), delta between -0.5 and 0
    cands = [p for p in exp["puts"] if p["strike"] < spot and -0.5 < p["delta"] < 0]

    picks, seen = [], set()
    for label, td in tiers:
        if not cands:
            break
        best = min(cands, key=lambda p: abs(abs(p["delta"]) - td))
        if best["strike"] in seen:
            continue
        seen.add(best["strike"])
        picks.append(_pick(label, td, best, spot, dte, contracts))
    picks.sort(key=lambda p: p["delta"])  # safe (low delta) first

    # full strike ladder (usable Δ band) so a UI slider can scrub every strike
    ladder = [_pick("", abs(p["delta"]), p, spot, dte, contracts)
              for p in sorted(cands, key=lambda x: x["strike"])
              if 0.04 <= abs(p["delta"]) <= 0.46]

    return {"status": "ok" if picks else "no_strike", "spot": round(spot, 2),
            "expiry": exp["expiry"], "dte": dte, "picks": picks, "ladder": ladder}


def recommend_csps_multi(chain: dict, symbol: str, contracts: int = 1,
                         buckets=DTE_BUCKETS, default: str = DEFAULT_BUCKET) -> dict:
    """Price the CSP ladder at several DTE targets from ONE chain fetch.

    Returns a term-structure payload (one bucket per DTE) so the frontend toggles
    client-side. Each bucket carries the Moderate-tier annualized yield so the DTE
    tradeoff (premium/cycle vs annualized) is directly comparable, like the covered
    -call term structure.
    """
    spot = chain.get("underlying")
    out = []
    for key, dte in buckets:
        r = recommend_csps(chain, target_dte=dte, contracts=contracts)
        mod = next((p for p in r.get("picks", []) if p["label"] == "Moderate"),
                   (r.get("picks") or [None])[0])
        out.append({
            "key": key, "target_dte": dte, "median_dte": r.get("dte", dte),
            "status": r.get("status"), "expiry": r.get("expiry"),
            "picks": r.get("picks", []), "ladder": r.get("ladder", []),
            "cycle_roi_pct": (mod or {}).get("static_yield_pct"),   # actual return for THIS DTE
            "annual_yield_pct": (mod or {}).get("annual_yield_pct"),
            "cycle_premium": (mod or {}).get("total_premium"),
        })
    return {
        "symbol": symbol.upper(), "spot": round(spot, 2) if spot else None,
        "contracts": contracts, "near_money_delta": NEAR_MONEY_DELTA,
        "buckets": out, "default": default,
        "status": "ok" if (spot and any(b["picks"] for b in out)) else "no_chain",
    }
