"""Covered-call advisor — pure functions over Book + live option chains.

The locked rule: a covered call is NEVER written below cost basis. Every
recommendation therefore has strike >= cost basis, which guarantees a positive
if-called return (strike - cost + premium) — i.e. the book only writes calls
that make money if assigned. Each candidate is risk-assessed (annualized yield,
if-called return, downside cushion, assignment probability from delta).

Premium collected = the current BID (a realistic sell fill), never the mark.
"""
from __future__ import annotations

from ingest.models import Book

# (label, target delta) — postures for the wheel. Conservative..Balanced are the
# owner's comfort zone; "Aggressive" (0.30) is near-the-money and flagged as a
# call-away risk the owner avoids.
DELTA_TARGETS = [("Conservative", 0.15), ("Balanced", 0.20), ("Aggressive", 0.30)]

# delta at/above this reads as "near the money" -> elevated call-away risk (avoid).
NEAR_MONEY_DELTA = 0.30

# DTE buckets for the term-structure toggle: (label, target DTE). One chain fetch
# feeds all of them — each bucket just snaps to its nearest available expiry.
DTE_BUCKETS = [("2w", 14), ("3w", 21), ("4w", 28), ("5w", 35)]
DEFAULT_BUCKET = "4w"


def _pick(label, target_delta, c, cost_basis, contracts, dte, spot):
    """Build one risk-assessed recommendation from a chain call `c`."""
    prem = c["bid"]
    total = round(prem * 100 * contracts, 2)
    static = prem / cost_basis if cost_basis else 0.0
    ann = static * 365 / dte if dte else 0.0
    if_called = (c["strike"] - cost_basis + prem) / cost_basis if cost_basis else 0.0
    if_called_ann = if_called * 365 / dte if dte else 0.0
    downside = prem / spot if spot else 0.0
    upside_cap = (c["strike"] - spot) / spot if spot else 0.0
    return {
        "label": label, "target_delta": target_delta,
        "strike": c["strike"], "delta": round(c["delta"], 3), "dte": dte,
        "premium": round(prem, 2), "total_premium": total,
        "open_interest": c.get("oi", 0),
        "static_yield_pct": round(static * 100, 2),
        "annual_yield_pct": round(ann * 100, 1),
        "if_called_return_pct": round(if_called * 100, 2),
        "if_called_annual_pct": round(if_called_ann * 100, 1),
        "downside_pct": round(downside * 100, 2),
        "assignment_prob_pct": round(c["delta"] * 100, 1),
        "breakeven": round(spot - prem, 2) if spot else None,
        "upside_cap_pct": round(upside_cap * 100, 2),
        # near-the-money => higher chance of being called away (owner avoids)
        "near_money": c["delta"] >= NEAR_MONEY_DELTA,
    }


def recommend_covered_calls(
    book: Book, chains: dict, target_dte: int = 35, target_deltas=DELTA_TARGETS,
) -> dict:
    """Recommend covered calls per eligible holding, honoring the cost-basis rule.

    `chains` is the output of `fetch_call_chains`. Returns per-stock records with
    a status and (when available) three delta-tiered picks, plus a portfolio roll-up.
    """
    # shares already committed to short calls, per underlying
    covered: dict[str, int] = {}
    for o in book.short_calls:
        covered[o.symbol] = covered.get(o.symbol, 0) + abs(int(round(o.qty)))

    recs = []
    for s in sorted(book.stocks, key=lambda x: -x.market_value):
        lots = int(s.qty // 100)
        if lots < 1:
            continue
        avail = lots - covered.get(s.symbol, 0)
        base = {
            "symbol": s.symbol, "shares": s.qty, "lots": lots,
            "covered_lots": covered.get(s.symbol, 0), "contracts": max(avail, 0),
            "cost_basis": round(s.cost_basis, 2), "mark": round(s.mark, 2),
        }
        if avail < 1:
            recs.append({**base, "status": "fully_covered", "picks": []})
            continue

        ch = chains.get(s.symbol) or {}
        exps = ch.get("expirations") or []
        spot = ch.get("underlying") or s.mark
        base["spot"] = round(spot, 2)
        base["underwater"] = spot < s.cost_basis
        if not exps:
            recs.append({**base, "status": "no_chain", "picks": []})
            continue

        exp = min(exps, key=lambda e: abs(e["dte"] - target_dte))
        dte = exp["dte"]
        # candidates: OTM calls (delta < 0.5) struck AT/ABOVE cost basis
        cands = [c for c in exp["calls"]
                 if c["strike"] >= s.cost_basis and 0 < c["delta"] < 0.5]

        picks, seen = [], set()
        for label, td in target_deltas:
            if not cands:
                break
            best = min(cands, key=lambda c: abs(c["delta"] - td))
            if best["strike"] in seen:
                continue
            seen.add(best["strike"])
            picks.append(_pick(label, td, best, s.cost_basis, avail, dte, spot))
        picks.sort(key=lambda p: p["strike"])

        # income left on the table on the UNCOVERED contracts — a Conservative..
        # Balanced range (the owner's comfort zone; excludes near-the-money).
        by_label = {p["label"]: p for p in picks}
        cons = by_label.get("Conservative")
        bal = by_label.get("Balanced") or cons
        cons = cons or bal
        income_left = None
        if cons and bal:
            lo, hi = sorted((cons["total_premium"], bal["total_premium"]))
            income_left = {
                "low": lo, "high": hi,
                "annual_low": round(lo * 365 / dte) if dte else 0,
                "annual_high": round(hi * 365 / dte) if dte else 0,
                "strike_low": cons["strike"], "strike_high": bal["strike"],
            }

        recs.append({
            **base, "expiry": exp["expiry"], "dte": dte,
            "status": "ok" if picks else "no_strike_above_cost", "picks": picks,
            "income_left": income_left,
        })

    # portfolio roll-up on the Balanced tier (fallback to first pick)
    def balanced(r):
        for p in r["picks"]:
            if p["label"] == "Balanced":
                return p
        return r["picks"][0] if r["picks"] else None

    actionable = [r for r in recs if r["status"] == "ok"]
    total_prem = round(sum((balanced(r) or {}).get("total_premium", 0) for r in actionable), 2)
    il = [r["income_left"] for r in actionable if r.get("income_left")]
    return {
        "as_of": book.as_of.isoformat(),
        "target_dte": target_dte,
        "near_money_delta": NEAR_MONEY_DELTA,
        "recommendations": recs,
        "summary": {
            "actionable": len(actionable),
            "fully_covered": sum(1 for r in recs if r["status"] == "fully_covered"),
            "no_chain": sum(1 for r in recs if r["status"] == "no_chain"),
            "underwater": sum(1 for r in recs if r.get("underwater")),
            "total_premium_balanced": total_prem,
            # income currently left on the table (uncovered), per cycle + annualized
            "income_left_low": round(sum(x["low"] for x in il), 2),
            "income_left_high": round(sum(x["high"] for x in il), 2),
            "income_left_annual_low": round(sum(x["annual_low"] for x in il)),
            "income_left_annual_high": round(sum(x["annual_high"] for x in il)),
        },
    }


def recommend_covered_calls_multi(
    book: Book, chains: dict, buckets=DTE_BUCKETS, default: str = DEFAULT_BUCKET,
) -> dict:
    """Price the covered-call board at several DTE targets from ONE chain fetch.

    Returns a term-structure payload: one bucket per (label, DTE). The frontend
    toggles between buckets client-side — no refetch. Each bucket carries a
    `median_dte` (the typical actual DTE it snapped to) so the UI can be honest
    when a target lands on the same expiry as its neighbor (e.g. monthly-only
    names). Also computes an `annual_premium_balanced` so the term structure
    (premium/cycle vs. annualized) is directly comparable across durations.
    """
    out = []
    for key, dte in buckets:
        r = recommend_covered_calls(book, chains, target_dte=dte)
        actionable = [rec for rec in r["recommendations"] if rec["status"] == "ok"]
        dtes = sorted(rec["dte"] for rec in actionable if rec.get("dte"))
        median_dte = dtes[len(dtes) // 2] if dtes else dte

        # annualize the balanced premium so shorter cycles' faster rewrite shows
        def _bal(rec):
            for p in rec["picks"]:
                if p["label"] == "Balanced":
                    return p
            return rec["picks"][0] if rec["picks"] else None

        ann = 0.0
        for rec in actionable:
            p = _bal(rec)
            if p and rec.get("dte"):
                ann += p["total_premium"] * 365 / rec["dte"]
        r["summary"]["annual_premium_balanced"] = round(ann)
        out.append({
            "key": key, "target_dte": dte, "median_dte": median_dte,
            "recommendations": r["recommendations"], "summary": r["summary"],
        })
    return {
        "as_of": book.as_of.isoformat(),
        "near_money_delta": NEAR_MONEY_DELTA,
        "buckets": out,
        "default": default,
    }
