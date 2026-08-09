"""Gamma-exposure (GEX) analytics — pure functions over a full option chain.

Computed entirely from the live Schwab chain (gamma + open interest per strike),
so no third-party GEX feed is needed. Dealer convention (SqueezeMetrics-style):
dealers are assumed LONG call gamma and SHORT put gamma, so calls add positive
GEX and puts add negative GEX.

Key outputs per underlying:
  * net GEX ($ per 1% move) and its sign (positive = vol-dampening / mean-revert,
    negative = vol-amplifying / trend),
  * the zero-gamma "flip" spot — recomputed by re-pricing every contract's gamma
    with Black-Scholes across candidate spots (uses each contract's own IV/DTE),
  * the call wall / put wall — the strikes carrying the most call / put gamma,
  * a per-strike GEX profile for the chart.

No I/O; unit-testable against a synthetic chain.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

CONTRACT = 100          # shares per option contract
RISK_FREE = 0.04        # flat short rate for the BS reprice
DEFAULT_IV = 0.20       # fallback when a contract has no reported IV
BAND = 0.20             # keep strikes within ±20% of spot (gamma is negligible beyond)
WEAK_OI = 200           # OI at a wall strike below this -> "weak" (single-stock noise)
MIN_CHAIN_OI = 250      # total chain OI below this -> insufficient options data (illiquid)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_gamma(spot: float, strike: float, t_years: float, sigma: float,
              r: float = RISK_FREE) -> float:
    """Black-Scholes gamma (same for a call and a put). 0 in degenerate cases."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return 0.0
    vt = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / vt
    return _norm_pdf(d1) / (spot * vt)


def _dollar_gamma(gamma: float, oi: float, spot: float) -> float:
    """Dealer $ gamma exposure for one strike's OI, per 1% move in spot."""
    return gamma * oi * CONTRACT * spot * spot * 0.01


@dataclass
class GexStrike:
    strike: float
    call_gex: float = 0.0
    put_gex: float = 0.0
    call_oi: float = 0.0
    put_oi: float = 0.0

    @property
    def net_gex(self) -> float:
        return self.call_gex - self.put_gex   # dealer: +call gamma, -put gamma


@dataclass
class GexResult:
    symbol: str
    spot: float
    net_gex: float
    call_gex_total: float
    put_gex_total: float
    flip: float | None
    call_wall: float | None
    put_wall: float | None
    call_wall_gex: float
    put_wall_gex: float
    call_wall_oi: float = 0.0
    put_wall_oi: float = 0.0
    call_wall_weak: bool = False
    put_wall_weak: bool = False
    total_oi: float = 0.0
    strikes: list = field(default_factory=list)
    expiries_available: list = field(default_factory=list)
    expiries_used: list = field(default_factory=list)
    contracts: int = 0

    @property
    def regime(self) -> str:
        return "positive" if self.net_gex >= 0 else "negative"

    @property
    def flip_pct(self) -> float | None:
        if self.flip is None or not self.spot:
            return None
        return round((self.flip - self.spot) / self.spot * 100, 2)


def _select(chain: dict, expiries) -> list[dict]:
    exps = chain.get("expirations") or []
    if not expiries:
        return exps
    want = set(expiries)
    return [e for e in exps if e.get("expiry") in want]


def _median_iv(exps: list[dict]) -> float:
    ivs = [c["iv"] for e in exps for c in (e.get("calls", []) + e.get("puts", []))
           if c.get("iv")]
    if not ivs:
        return DEFAULT_IV
    ivs.sort()
    return ivs[len(ivs) // 2]


def _flip_level(exps: list[dict], spot: float, med_iv: float) -> float | None:
    """Zero-gamma spot: scan candidate spots, reprice gamma with BS, find the
    crossing where net dealer gamma flips from negative to positive."""
    if spot <= 0:
        return None
    contracts = []
    for e in exps:
        t = max((e.get("dte") or 0), 0) / 365.0
        for c in e.get("calls", []):
            contracts.append((c["strike"], c["oi"], c.get("iv") or med_iv, t, +1))
        for p in e.get("puts", []):
            contracts.append((p["strike"], p["oi"], p.get("iv") or med_iv, t, -1))
    if not contracts:
        return None

    def net_at(s: float) -> float:
        tot = 0.0
        for strike, oi, iv, t, sign in contracts:
            g = _bs_gamma(s, strike, t, iv)
            tot += sign * _dollar_gamma(g, oi, s)
        return tot

    lo, hi = spot * (1 - 0.25), spot * (1 + 0.25)
    steps = 120
    prev_s = lo
    prev_n = net_at(lo)
    for i in range(1, steps + 1):
        s = lo + (hi - lo) * i / steps
        n = net_at(s)
        # a real crossing is a sign change; identically-flat net (e.g. matched
        # call/put gamma) has no meaningful flip -> fall through to None.
        if (prev_n < 0) != (n < 0) and n != prev_n:
            root = prev_s + (s - prev_s) * (0 - prev_n) / (n - prev_n)
            return round(root, 2)
        prev_s, prev_n = s, n
    return None


def compute_gex(chain: dict, symbol: str, expiries=None, band: float = BAND) -> GexResult:
    """Aggregate GEX for `symbol` from a full chain, optionally limited to
    `expiries` (list of 'YYYY-MM-DD'). Strikes are kept within ±band of spot."""
    spot = chain.get("underlying") or 0.0
    all_exps = chain.get("expirations") or []
    avail = [{"expiry": e["expiry"], "dte": e["dte"]} for e in all_exps]
    exps = _select(chain, expiries)

    lo, hi = spot * (1 - band), spot * (1 + band)
    by_strike: dict[float, GexStrike] = {}
    n = 0
    for e in exps:
        for c in e.get("calls", []):
            if spot and not (lo <= c["strike"] <= hi):
                continue
            gs = by_strike.setdefault(c["strike"], GexStrike(c["strike"]))
            gs.call_gex += _dollar_gamma(c["gamma"], c["oi"], spot)
            gs.call_oi += c["oi"]
            n += 1
        for p in e.get("puts", []):
            if spot and not (lo <= p["strike"] <= hi):
                continue
            gs = by_strike.setdefault(p["strike"], GexStrike(p["strike"]))
            gs.put_gex += _dollar_gamma(p["gamma"], p["oi"], spot)
            gs.put_oi += p["oi"]
            n += 1

    strikes = [by_strike[k] for k in sorted(by_strike)]
    call_total = sum(s.call_gex for s in strikes)
    put_total = sum(s.put_gex for s in strikes)
    total_oi = sum(s.call_oi + s.put_oi for s in strikes)

    # Classic walls: call wall = biggest call gamma AT/ABOVE spot (overhead
    # resistance); put wall = biggest put gamma AT/BELOW spot (downside support).
    # Fall back to the whole band if nothing sits on the expected side.
    call_pool = [s for s in strikes if not spot or s.strike >= spot] or strikes
    put_pool = [s for s in strikes if not spot or s.strike <= spot] or strikes
    call_wall = max(call_pool, key=lambda s: s.call_gex, default=None) if strikes else None
    put_wall = max(put_pool, key=lambda s: s.put_gex, default=None) if strikes else None
    med_iv = _median_iv(exps)
    flip = _flip_level(exps, spot, med_iv) if strikes else None

    return GexResult(
        symbol=symbol.upper(), spot=round(spot, 2),
        net_gex=round(call_total - put_total, 2),
        call_gex_total=round(call_total, 2), put_gex_total=round(-put_total, 2),
        flip=flip,
        call_wall=call_wall.strike if call_wall else None,
        put_wall=put_wall.strike if put_wall else None,
        call_wall_gex=round(call_wall.call_gex, 2) if call_wall else 0.0,
        put_wall_gex=round(-put_wall.put_gex, 2) if put_wall else 0.0,
        call_wall_oi=round(call_wall.call_oi) if call_wall else 0.0,
        put_wall_oi=round(put_wall.put_oi) if put_wall else 0.0,
        call_wall_weak=bool(call_wall and call_wall.call_oi < WEAK_OI),
        put_wall_weak=bool(put_wall and put_wall.put_oi < WEAK_OI),
        total_oi=round(total_oi),
        strikes=[{"strike": s.strike, "call_gex": round(s.call_gex, 2),
                  "put_gex": round(-s.put_gex, 2), "net_gex": round(s.net_gex, 2)}
                 for s in strikes],
        expiries_available=avail,
        expiries_used=[e["expiry"] for e in exps],
        contracts=n,
    )


def _g(x: float) -> str:
    """Compact strike/price format: 770, 222.5, 1.5 — no trailing zeros."""
    return f"{x:g}"


def bias_read(res: GexResult) -> dict | None:
    """Plain-words market-maker bias from spot / flip / walls / net GEX.

    Context, NOT a trade signal. Returns {regime, regime_note, warn, range_note,
    trigger, color, headline} or None if there's no spot to reason from.
    """
    spot, flip = res.spot, res.flip
    cw, pw, net = res.call_wall, res.put_wall, res.net_gex
    if not spot:
        return None

    if flip is not None:
        distance = (spot - flip) / spot
        stabilizing = spot > flip
        near = abs(distance) < 0.005            # within 0.5% of the flip
    else:
        stabilizing = net >= 0                  # fall back to net-gamma sign
        near = False

    regime = "STABILIZING" if stabilizing else "AMPLIFYING"
    regime_note = ("dealers dampen · pin & mean-revert · fade extremes" if stabilizing
                   else "dealers amplify · trends & volatility · don't fade")
    warn = "NEAR FLIP — regime unstable, watch for a break" if near else None

    if cw and spot >= cw * 0.997:
        range_note = f"at call wall {_g(cw)} — resistance, fade longs"
    elif pw and spot <= pw * 1.003:
        range_note = f"at put wall {_g(pw)} — support, dips buyable"
    elif cw and pw:
        range_note = f"boxed between put wall {_g(pw)} and call wall {_g(cw)}"
    else:
        range_note = None

    trigger = None
    if flip is not None:
        trigger = (f"flips bearish below {_g(flip)}" if stabilizing
                   else f"flips bullish above {_g(flip)}")

    color = "amber" if near else ("green" if stabilizing else "red")
    headline = (f"spot {_g(spot)} {'>' if stabilizing else '<'} flip {_g(flip)}"
                if flip is not None else
                f"net GEX {'positive' if stabilizing else 'negative'}")
    return {"regime": regime, "regime_note": regime_note, "warn": warn,
            "range_note": range_note, "trigger": trigger, "color": color,
            "headline": headline}


def chain_total_oi(chain: dict) -> float:
    """Total open interest across the whole chain — a liquidity gate for GEX."""
    tot = 0.0
    for e in chain.get("expirations") or []:
        for leg in e.get("calls", []) + e.get("puts", []):
            tot += leg.get("oi", 0) or 0
    return tot
