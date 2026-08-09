"""Analytics engine — pure functions over a normalized `Book`.

No I/O, no globals, fully deterministic (uses `book.as_of` as "today"), so every
number is unit-testable against the frozen Jul-31 fixture.

Methodology (locked — this IS the product):
  * CSP exposure is NOTIONAL (strike*100*|qty|), never the option mark.
  * Real cash = Cash & Sweep + money-market only (never Option/Intraday BP).
  * P&L lives in separate universes: stock / CSP / covered-call / LEAP. Never blended.
  * LEAP = long call with > 182 DTE; <= 182 DTE = "other long".
  * Assignment waterfall: short puts by expiry, each assigns, drain cash first,
    margin loan starts only when cash hits 0.
  * VXN posture bands map a volatility reading to a target cash range.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ingest.models import Book, OptionPos

from .reference import (
    BROAD_SECTORS,
    HIGH_BETA,
    broad_of,
    is_high_beta,
    resolve as resolve_meta,
)

# A long call with strictly more DTE than this counts as a LEAP/Call.
# (Owner override of the original 182-day spec: anything over 90 days.)
LEAP_DTE_THRESHOLD = 90


# --------------------------------------------------------------------------- #
# Buckets — the 4 (+1) universes, never blended
# --------------------------------------------------------------------------- #
@dataclass
class Bucket:
    name: str
    count: int
    market_value: float  # signed dollar MV (negative for short options)
    pl_open: float
    notional: float = 0.0  # meaningful for CSP exposure


@dataclass
class Buckets:
    stock: Bucket
    csp: Bucket           # short puts
    covered_call: Bucket  # short calls
    leap: Bucket          # long calls > 182 DTE
    other_long: Bucket    # long calls <= 182 DTE
    # cross-bucket context
    real_cash: float
    net_liq: float
    gross_assets: float       # stock MV + long-option MV + real cash
    stock_pct_gross: float    # fraction (0..1)
    cash_pct_netliq: float    # fraction (0..1)


def _sum_mv(positions: list[OptionPos]) -> float:
    return round(sum(o.market_value for o in positions), 2)


def _sum_pl(positions: list[OptionPos]) -> float:
    return round(sum(o.pl_open for o in positions), 2)


def buckets(book: Book) -> Buckets:
    """Split the book into the locked universes and compute headline ratios."""
    today = book.as_of

    stock_mv = round(sum(s.market_value for s in book.stocks), 2)
    stock_pl = round(sum(s.pl_open for s in book.stocks), 2)

    short_puts = book.short_puts
    short_calls = book.short_calls
    long_calls = book.long_calls
    leaps = [o for o in long_calls if o.dte(today) > LEAP_DTE_THRESHOLD]
    others = [o for o in long_calls if o.dte(today) <= LEAP_DTE_THRESHOLD]

    csp = Bucket("CSP", len(short_puts), _sum_mv(short_puts), _sum_pl(short_puts),
                 notional=round(sum(o.notional for o in short_puts), 2))
    covered_call = Bucket("Covered Call", len(short_calls), _sum_mv(short_calls),
                          _sum_pl(short_calls),
                          notional=round(sum(o.notional for o in short_calls), 2))
    leap = Bucket("LEAPs/Calls", len(leaps), _sum_mv(leaps), _sum_pl(leaps),
                  notional=round(sum(o.notional for o in leaps), 2))
    other_long = Bucket("Other Long", len(others), _sum_mv(others), _sum_pl(others),
                        notional=round(sum(o.notional for o in others), 2))
    stock = Bucket("Stock", len(book.stocks), stock_mv, stock_pl)

    real_cash = book.balances.real_cash
    net_liq = book.balances.net_liq
    long_opt_mv = leap.market_value + other_long.market_value
    gross_assets = round(stock_mv + long_opt_mv + real_cash, 2)

    return Buckets(
        stock=stock, csp=csp, covered_call=covered_call, leap=leap, other_long=other_long,
        real_cash=real_cash, net_liq=net_liq, gross_assets=gross_assets,
        stock_pct_gross=(stock_mv / gross_assets) if gross_assets else 0.0,
        cash_pct_netliq=(real_cash / net_liq) if net_liq else 0.0,
    )


# --------------------------------------------------------------------------- #
# Assignment waterfall
# --------------------------------------------------------------------------- #
@dataclass
class WaterfallStep:
    symbol: str
    strike: float
    expiry: str
    contracts: int
    assign_cost: float      # notional pulled in when this leg assigns
    cumulative_cost: float
    cash_remaining: float
    margin_loan: float


@dataclass
class WaterfallResult:
    steps: list[WaterfallStep]
    starting_cash: float
    total_assign_cost: float
    peak_loan: float
    ending_cash: float
    margin_rate: float = 0.0        # annual, decimal
    annual_interest: float = 0.0    # $ / yr on the peak loan
    monthly_interest: float = 0.0
    daily_interest: float = 0.0


def assignment_waterfall(book: Book, margin_rate: float = 0.12) -> WaterfallResult:
    """Assume every short put assigns, in expiry order; drain cash, then borrow.

    A margin loan begins only once real cash reaches 0. Peak loan is the maximum
    loan drawn across the sequence (monotonic here, so == final loan). Interest is
    the carry cost of that peak loan at `margin_rate` (annual, decimal).
    """
    puts = sorted(book.short_puts, key=lambda o: o.expiry)
    cash = book.balances.real_cash

    remaining = cash
    loan = 0.0
    peak_loan = 0.0
    cumulative = 0.0
    steps: list[WaterfallStep] = []

    for o in puts:
        cost = o.notional
        cumulative = round(cumulative + cost, 2)
        if remaining >= cost:
            remaining = round(remaining - cost, 2)
        else:
            shortfall = round(cost - remaining, 2)
            remaining = 0.0
            loan = round(loan + shortfall, 2)
        peak_loan = max(peak_loan, loan)
        steps.append(WaterfallStep(
            symbol=o.symbol, strike=o.strike, expiry=o.expiry.isoformat(),
            contracts=int(abs(o.qty)), assign_cost=cost, cumulative_cost=cumulative,
            cash_remaining=remaining, margin_loan=loan,
        ))

    annual_int = round(peak_loan * margin_rate, 2)
    return WaterfallResult(
        steps=steps, starting_cash=cash,
        total_assign_cost=cumulative, peak_loan=round(peak_loan, 2),
        ending_cash=remaining,
        margin_rate=margin_rate,
        annual_interest=annual_int,
        monthly_interest=round(annual_int / 12, 2),
        daily_interest=round(annual_int / 365, 2),
    )


# --------------------------------------------------------------------------- #
# Margin snapshot
# --------------------------------------------------------------------------- #
@dataclass
class MarginSnapshot:
    net_liq: float
    real_cash: float
    margin_loan: float            # currently drawn
    option_buying_power: float    # NOT cash — shown for context only
    loan_if_all_assigned: float   # hypothetical peak loan from the waterfall


def margin_snapshot(book: Book) -> MarginSnapshot:
    """Current margin state plus the hypothetical loan if all short puts assign."""
    wf = assignment_waterfall(book)
    b = book.balances
    return MarginSnapshot(
        net_liq=b.net_liq,
        real_cash=b.real_cash,
        margin_loan=b.margin_loan,
        option_buying_power=b.option_buying_power,
        loan_if_all_assigned=wf.peak_loan,
    )


# --------------------------------------------------------------------------- #
# Covered-call guardrail: never written below cost basis
# --------------------------------------------------------------------------- #
@dataclass
class CoveredCallViolation:
    symbol: str
    strike: float
    expiry: str
    cost_basis: float


def covered_calls_below_basis(book: Book) -> list[CoveredCallViolation]:
    """Flag any short call struck BELOW the underlying's cost basis.

    Covered calls must never be written below cost basis; this surfaces breaches
    (and is the guardrail any future CC planner must enforce).
    """
    basis = {s.symbol: s.cost_basis for s in book.stocks}
    out: list[CoveredCallViolation] = []
    for o in book.short_calls:
        cb = basis.get(o.symbol)
        if cb is not None and o.strike < cb:
            out.append(CoveredCallViolation(o.symbol, o.strike, o.expiry.isoformat(), cb))
    return out


# --------------------------------------------------------------------------- #
# VXN posture (placeholder feed, real bands)
# --------------------------------------------------------------------------- #
# (lo_inclusive, hi_exclusive, band, (target_low_pct, target_high_pct))
# VXN (Nasdaq-100 vol) — the locked methodology bands.
_VXN_BANDS = [
    (float("-inf"), 20.0, "risk-on", (5.0, 10.0)),
    (20.0, 25.0, "caution", (10.0, 15.0)),
    (25.0, 30.0, "fear", (15.0, 25.0)),
    (30.0, 35.0, "elevated", (25.0, 35.0)),
    (35.0, float("inf"), "panic", (0.0, 10.0)),  # deploy down to ~10%
]

# VIX (S&P-500 vol) — calibrated to VIX levels (structurally lower than VXN), so
# a "normal" VIX ~15-20 already warrants a higher cash target than VXN's would.
_VIX_BANDS = [
    (float("-inf"), 15.0, "risk-on", (5.0, 10.0)),
    (15.0, 20.0, "caution", (10.0, 15.0)),
    (20.0, 25.0, "fear", (15.0, 20.0)),
    (25.0, 30.0, "elevated", (20.0, 30.0)),
    (30.0, float("inf"), "panic", (0.0, 10.0)),  # deploy down to ~10%
]


@dataclass
class Posture:
    kind: str            # "VXN" | "VIX"
    value: float
    band: str
    target_low_pct: float
    target_high_pct: float
    cash_pct: float | None = None   # gross real cash % of net liq, if assessed
    status: str | None = None       # "below" | "in range" | "above"
    free_pct: float | None = None   # free (no-margin) cash % = dry powder / net liq
    free_status: str | None = None  # band status of the free-cash figure


VxnPosture = Posture  # backwards-compatible alias


def _classify(pct_display: float, lo: float, hi: float) -> str:
    if pct_display < lo:
        return "below"
    if pct_display > hi:
        return "above"
    return "in range"


def _posture(kind: str, value: float, bands: list, cash_pct: float | None = None,
             free_pct: float | None = None) -> Posture:
    """Map a volatility reading to its band + target cash range.

    If `cash_pct` (fraction of net liq, 0..1) is given, also classify whether
    current cash is below / in range / above the target band. `free_pct` is the
    same, for free (no-margin) cash = dry powder / net liq — classified against
    the SAME target band so both reads are directly comparable.
    """
    band, lo, hi = bands[0][2], bands[0][3][0], bands[0][3][1]
    for blo, bhi, name, (tlo, thi) in bands:
        if blo <= value < bhi:
            band, lo, hi = name, tlo, thi
            break

    status = pct_display = None
    if cash_pct is not None:
        pct_display = round(cash_pct * 100, 1)
        status = _classify(pct_display, lo, hi)

    free_status = free_display = None
    if free_pct is not None:
        free_display = round(free_pct * 100, 1)
        free_status = _classify(free_display, lo, hi)

    return Posture(kind=kind, value=value, band=band, target_low_pct=lo,
                   target_high_pct=hi, cash_pct=pct_display, status=status,
                   free_pct=free_display, free_status=free_status)


def vxn_posture(vxn: float, cash_pct: float | None = None,
                free_pct: float | None = None) -> Posture:
    """VXN (Nasdaq-100 vol) posture — the locked methodology bands."""
    return _posture("VXN", vxn, _VXN_BANDS, cash_pct, free_pct)


def vix_posture(vix: float, cash_pct: float | None = None,
                free_pct: float | None = None) -> Posture:
    """VIX (S&P-500 vol) posture — VIX-calibrated bands (lower than VXN's)."""
    return _posture("VIX", vix, _VIX_BANDS, cash_pct, free_pct)


def band_segments(kind: str) -> list[dict]:
    """Display segments for the VIX/VXN reference diagram — one per band, with a
    finite display range (open-ended ends get a typical-width span) so the frontend
    can draw equal segments and place the 'you are here' marker by interpolation.
    """
    bands = _VXN_BANDS if kind.upper() == "VXN" else _VIX_BANDS
    # typical band width (the middle bands are all this wide) -> use for open ends
    widths = [hi - lo for lo, hi, *_ in bands if lo != float("-inf") and hi != float("inf")]
    w = widths[0] if widths else 5.0
    out = []
    for lo, hi, band, (tl, th) in bands:
        if lo == float("-inf"):
            dlo, dhi, label, open_ = hi - w, hi, f"< {hi:.0f}", "low"
        elif hi == float("inf"):
            dlo, dhi, label, open_ = lo, lo + w, f"> {lo:.0f}", "high"
        else:
            dlo, dhi, label, open_ = lo, hi, f"{lo:.0f}–{hi:.0f}", None
        out.append({"lo": dlo, "hi": dhi, "band": band, "label": label, "open": open_,
                    "target_low": tl, "target_high": th})
    return out


def sector_analysis(book: Book, live: dict | None = None) -> dict:
    """Group the WHOLE book (stocks + options) by sector, per underlying.

    Every position contributes its natural exposure measure — consistent with
    the Command buckets:
        stock             -> market value
        LEAP/long call    -> market value
        CSP (short put)   -> notional
        covered call      -> notional
    Positions are aggregated by underlying symbol (so a symbol's stock + puts +
    calls roll into one holding), grouped by sector, with a component breakdown,
    beta flag, and leveraged flag. Base = gross exposure (sum of all).

    `live` (optional) is {symbol: {"sector":..., "beta":...}} from Finnhub; it
    feeds `reference.resolve()`, where the owner SECTOR_OVERRIDE still wins.
    """
    live = live or {}
    # Accumulate exposure components per underlying symbol.
    # NOTE: covered calls are NOT exposure — the shares are already counted as
    # stock; a CC's notional would double-count the same underlying. CCs are kept
    # as informational fields only (they cap upside, they don't add allocation).
    comp: dict[str, dict] = {}

    def slot(sym: str) -> dict:
        return comp.setdefault(sym, {"stock": 0.0, "csp": 0.0, "call": 0.0, "spread": 0.0,
                                     "cc_notional": 0.0, "cc_mark": 0.0, "cc_contracts": 0})

    # Spread legs are hedged — they must NOT be counted as naked CSP/CC/long-call.
    # The spread is folded in as its own strategy, measured by defined MAX LOSS
    # (capital at risk), exactly as CSP is measured by notional.
    spread_legs = book._spread_leg_ids
    for sp in book.spreads:
        slot(sp.symbol)["spread"] += sp.max_loss

    for s in book.stocks:
        slot(s.symbol)["stock"] += s.market_value
    for o in book.options:
        if id(o) in spread_legs:
            continue                                      # part of a spread — handled above
        sl = slot(o.symbol)
        if o.kind == "PUT" and o.is_short:
            sl["csp"] += o.notional                      # real assignment obligation
        elif o.kind == "CALL" and o.is_short:
            sl["cc_notional"] += o.notional               # informational only
            sl["cc_mark"] += o.market_value               # short liability (small, negative)
            sl["cc_contracts"] += abs(int(round(o.qty)))
        elif o.kind == "CALL" and o.is_long:
            sl["call"] += o.market_value
        # naked long puts are rare in this book; folded out of scope for now

    total = 0.0
    holdings = []
    for sym, c in comp.items():
        # exposure EXCLUDES covered-call notional (no double-count); spreads count
        # at defined max loss (capital at risk)
        exposure = round(c["stock"] + c["csp"] + c["call"] + c["spread"], 2)
        lv = live.get(sym, {})
        sector, beta, leveraged = resolve_meta(sym, lv.get("sector"), lv.get("beta"))
        holdings.append({
            "symbol": sym, "sector": sector, "beta": beta,
            "high_beta": is_high_beta(beta), "leveraged": leveraged, "exposure": exposure,
            "stock": round(c["stock"], 2), "csp": round(c["csp"], 2),
            "call": round(c["call"], 2), "spread": round(c["spread"], 2),
            # covered-call info (not in exposure): caps upside, small liability
            "cc_contracts": c["cc_contracts"],
            "cc_notional": round(c["cc_notional"], 2),
            "cc_mark": round(c["cc_mark"], 2),
            "capped": c["cc_contracts"] > 0,
        })
        total += exposure
    total = round(total, 2)

    sectors: dict[str, dict] = {}
    high_beta_value = 0.0
    leveraged_value = 0.0
    for h in holdings:
        if h["high_beta"]:
            high_beta_value += h["exposure"]
        if h["leveraged"]:
            leveraged_value += h["exposure"]
        g = sectors.setdefault(h["sector"], {"sector": h["sector"], "value": 0.0,
                                             "high_beta_value": 0.0, "holdings": []})
        g["value"] = round(g["value"] + h["exposure"], 2)
        if h["high_beta"]:
            g["high_beta_value"] = round(g["high_beta_value"] + h["exposure"], 2)
        g["holdings"].append(h)

    out = []
    for g in sectors.values():
        g["pct_of_total"] = _pct(g["value"], total)
        for h in g["holdings"]:
            h["pct_of_sector"] = _pct(h["exposure"], g["value"])
            h["pct_of_total"] = _pct(h["exposure"], total)
        g["holdings"].sort(key=lambda x: -x["exposure"])
        out.append(g)
    out.sort(key=lambda g: -g["value"])

    return {
        "gross_exposure": total,
        "net_liq": book.balances.net_liq,
        "high_beta_value": round(high_beta_value, 2),
        "high_beta_pct": _pct(high_beta_value, total),
        "high_beta_threshold": HIGH_BETA,
        "leveraged_value": round(leveraged_value, 2),
        "leveraged_pct": _pct(leveraged_value, total),
        "sector_count": len(out),
        "sectors": out,
        # Diversification: which broad GICS groups have zero exposure.
        "covered_broad": sorted({b for b in (broad_of(g["sector"]) for g in out) if b}),
        "missing_broad": [b for b in BROAD_SECTORS
                          if b not in {broad_of(g["sector"]) for g in out}],
    }


def get_vxn() -> float:
    """Placeholder VXN feed (Phase 5 wires a real source behind this seam)."""
    return 28.6


def get_vix() -> float:
    """Placeholder VIX feed (Phase 5 wires a real source behind this seam)."""
    return 24.5


# --------------------------------------------------------------------------- #
# One canonical, serializable summary (consumed by the store and the API)
# --------------------------------------------------------------------------- #
def summary(book: Book, vxn: float | None = None, vix: float | None = None,
            ytd_start_netliq: float = 0.0, ytd_flows: float = 0.0) -> dict:
    """Assemble every headline figure into one JSON-serializable dict.

    Single source of truth for the snapshot store (Phase 3) and the dashboard
    API (Phase 4), so both always agree with the engine. `vxn`/`vix` are the live
    index levels (from Schwab); when omitted they fall back to the stub feeds.
    YTD P/L = net liq - start-of-year net liq - net external cash flows (None if
    no baseline is configured).
    """
    ytd_pl = (round(book.balances.net_liq - ytd_start_netliq - ytd_flows, 2)
              if ytd_start_netliq and ytd_start_netliq > 0 else None)
    bk = buckets(book)
    ms = margin_snapshot(book)
    v = get_vxn() if vxn is None else vxn
    vv = get_vix() if vix is None else vix
    violations = covered_calls_below_basis(book)

    # Dry powder = cash NOT reserved against open short-put (CSP) assignment
    # obligations. This is the number that RISES when you buy back a CSP: closing
    # the put frees its notional even though the buyback debit lowers raw cash.
    # Negative => you're leaning on margin/buying-power to cover CSP assignment.
    csp_committed = bk.csp.notional
    dry_powder = round(bk.real_cash - csp_committed, 2)
    csp_coverage = round(bk.real_cash / csp_committed, 4) if csp_committed > 0 else None
    # Free (no-margin) cash % — dry powder over net liq. The posture grades BOTH
    # the gross read (all real cash) and this free read side by side.
    free_pct = (dry_powder / bk.net_liq) if bk.net_liq else None
    posture = vxn_posture(v, cash_pct=bk.cash_pct_netliq, free_pct=free_pct)
    posture_vix = vix_posture(vv, cash_pct=bk.cash_pct_netliq, free_pct=free_pct)

    return {
        "as_of": book.as_of.isoformat(),
        "net_liq": bk.net_liq,
        "gross_assets": bk.gross_assets,
        "stock_mv": bk.stock.market_value,
        "stock_pct_gross": round(bk.stock_pct_gross, 4),
        "real_cash": bk.real_cash,
        "cash_pct_netliq": round(bk.cash_pct_netliq, 4),
        "csp_committed": csp_committed,
        "dry_powder": dry_powder,
        "csp_coverage": csp_coverage,
        # Live P/L: today's day change + total unrealized + YTD (baseline-based)
        "day_pl": book.balances.day_pl,
        "open_pl": book.balances.open_pl,
        "ytd_pl": ytd_pl,
        "buckets": {
            "stock": asdict(bk.stock),
            "csp": asdict(bk.csp),
            "covered_call": asdict(bk.covered_call),
            "leap": asdict(bk.leap),
            "other_long": asdict(bk.other_long),
        },
        "margin": asdict(ms),
        "posture": {**asdict(posture), "bands": band_segments("VXN")},          # VXN (primary — drives cash_status)
        "posture_vix": {**asdict(posture_vix), "bands": band_segments("VIX")},  # VIX (paired display)
        "covered_call_violations": [asdict(x) for x in violations],
    }


# --------------------------------------------------------------------------- #
# Per-position % breakdown — powers the portfolio-% dashboard (main tab)
# --------------------------------------------------------------------------- #
def _pct(value: float, base: float) -> float:
    return round((value / base) * 100, 2) if base else 0.0


def positions_breakdown(book: Book) -> dict:
    """Every position with its share of the portfolio (base = Net Liq).

    Stocks / LEAPs / long calls are measured by market value; CSPs and covered
    calls by NOTIONAL exposure (the methodology's exposure measure). Each row
    carries both the dollar figure and its % of net liq.
    """
    today = book.as_of
    base = book.balances.net_liq

    stocks = [{
        "symbol": s.symbol, "qty": s.qty, "mark": s.mark,
        "value": s.market_value, "pct": _pct(s.market_value, base),
        "pl_open": s.pl_open, "measure": "market_value",
    } for s in sorted(book.stocks, key=lambda p: -p.market_value)]

    def opt_rows(positions, measure):
        rows = []
        for o in positions:
            value = o.notional if measure == "notional" else o.market_value
            rows.append({
                "symbol": o.symbol, "kind": o.kind, "contracts": int(o.qty),
                "strike": o.strike, "expiry": o.expiry.isoformat(),
                "dte": o.dte(today), "mark": o.mark,
                "value": value, "pct": _pct(abs(value), base),
                "pl_open": o.pl_open, "measure": measure,
            })
        return sorted(rows, key=lambda r: -r["pct"])

    long_calls = book.long_calls
    leaps = [o for o in long_calls if o.dte(today) > LEAP_DTE_THRESHOLD]
    others = [o for o in long_calls if o.dte(today) <= LEAP_DTE_THRESHOLD]

    groups = {
        "stock": {"measure": "market_value", "rows": stocks},
        "csp": {"measure": "notional", "rows": opt_rows(book.short_puts, "notional")},
        "covered_call": {"measure": "notional", "rows": opt_rows(book.short_calls, "notional")},
        "leap": {"measure": "market_value", "rows": opt_rows(leaps, "market_value")},
        "other_long": {"measure": "market_value", "rows": opt_rows(others, "market_value")},
    }
    for g in groups.values():
        g["total_value"] = round(sum(r["value"] for r in g["rows"]), 2)
        g["total_pct"] = round(sum(r["pct"] for r in g["rows"]), 2)

    # Vertical spreads — defined risk, measured by MAX LOSS (never naked notional).
    spreads = [{
        "symbol": s.symbol, "kind": s.kind, "label": s.label, "direction": s.direction,
        "long_strike": s.long_leg.strike, "short_strike": s.short_leg.strike,
        "contracts": s.contracts, "expiry": s.expiry.isoformat(), "dte": s.dte(today),
        "width": s.width, "is_debit": s.is_debit, "net_cost": s.net_cost,
        "max_loss": s.max_loss, "max_profit": s.max_profit, "breakeven": s.breakeven,
        "mark_value": s.mark_value, "pl_open": s.pl_open,
        "max_loss_pct": _pct(s.max_loss, base),
    } for s in sorted(book.spreads, key=lambda x: -x.max_loss)]

    csp_committed = round(sum(o.notional for o in book.short_puts), 2)
    return {
        "portfolio_base": base,
        "real_cash": book.balances.real_cash,
        "cash_pct": _pct(book.balances.real_cash, base),
        "csp_committed": csp_committed,
        "dry_powder": round(book.balances.real_cash - csp_committed, 2),
        "groups": groups,
        "spreads": spreads,
    }
