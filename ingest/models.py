"""Normalized data shapes — the single `Book` every downstream layer sees.

The data SOURCE (live Schwab vs offline fixture) is swappable behind
`load_book()`; nothing below this module knows or cares which one produced it.

Money conventions:
  - Option qty is signed: +long, -short.
  - CSP/exposure is always NOTIONAL (strike * 100 * abs(qty)), never the mark.
  - `mark` is the live per-unit price from the API (per share for stock,
    per contract-share for options — multiply by 100 for a dollar figure).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

Kind = Literal["PUT", "CALL"]

CONTRACT_MULTIPLIER = 100


@dataclass
class StockPos:
    symbol: str
    qty: float
    cost_basis: float  # per-share average cost
    mark: float        # live per-share price

    @property
    def market_value(self) -> float:
        return round(self.qty * self.mark, 2)

    @property
    def pl_open(self) -> float:
        """Unrealized P&L on the equity leg."""
        return round((self.mark - self.cost_basis) * self.qty, 2)


@dataclass
class OptionPos:
    symbol: str
    kind: Kind          # "PUT" | "CALL"
    qty: float          # signed: +long, -short
    strike: float
    expiry: date
    trade_price: float  # per-contract-share premium at open
    mark: float         # live per-contract-share price (needed for buyback/P&L)
    # Live P&L straight from the API when available; otherwise derived from mark.
    _pl_open: float | None = field(default=None, repr=False)

    @property
    def is_short(self) -> bool:
        return self.qty < 0

    @property
    def is_long(self) -> bool:
        return self.qty > 0

    def dte(self, today: date) -> int:
        """Calendar days to expiration as of `today`."""
        return (self.expiry - today).days

    @property
    def notional(self) -> float:
        """Contract notional — strike * 100 * abs(qty). The CSP exposure measure."""
        return round(self.strike * CONTRACT_MULTIPLIER * abs(self.qty), 2)

    def buyback_cost(self) -> float:
        """Dollar cost to close the position at the current mark."""
        return round(self.mark * CONTRACT_MULTIPLIER * abs(self.qty), 2)

    @property
    def market_value(self) -> float:
        """Signed dollar market value (positive for longs, negative for shorts)."""
        return round(self.mark * CONTRACT_MULTIPLIER * self.qty, 2)

    @property
    def pl_open(self) -> float:
        """Unrealized P&L. Uses the live API value if supplied, else derives it.

        Short: premium received minus current cost to close.
        Long:  current value minus premium paid.
        """
        if self._pl_open is not None:
            return round(self._pl_open, 2)
        per_share = (self.trade_price - self.mark) if self.is_short else (self.mark - self.trade_price)
        return round(per_share * CONTRACT_MULTIPLIER * abs(self.qty), 2)


@dataclass
class Balances:
    cash_and_sweep: float
    option_buying_power: float  # NOT cash — never summed into real cash
    net_liq: float
    margin_loan: float = 0.0
    # Sum of money-market holdings (SWVXX etc.) if present; often $0 or absent.
    money_market: float = 0.0
    # Live P/L straight from Schwab: today's change and total unrealized.
    day_pl: float = 0.0
    open_pl: float = 0.0

    @property
    def real_cash(self) -> float:
        """Deployable cash = Cash & Sweep + any money-market balances.

        Explicitly excludes Option/Intraday Buying Power.
        """
        return round(self.cash_and_sweep + self.money_market, 2)


@dataclass
class Book:
    stocks: list[StockPos]
    options: list[OptionPos]
    balances: Balances
    as_of: date

    # --- convenience selectors (pure, no I/O) ---
    # Legs that belong to a detected vertical spread are EXCLUDED from the naked
    # selectors below: a short put inside a spread is hedged, so it is NOT a
    # cash-secured put and must not be counted at full notional (CSP, waterfall,
    # sector, breakdown all read these). Spreads are surfaced via `spreads`.
    @property
    def spreads(self) -> list[Spread]:
        spreads, _ = detect_spreads(self.options)
        return spreads

    @property
    def _spread_leg_ids(self) -> set[int]:
        _, leg_ids = detect_spreads(self.options)
        return leg_ids

    @property
    def short_puts(self) -> list[OptionPos]:
        skip = self._spread_leg_ids
        return [o for o in self.options if o.kind == "PUT" and o.is_short and id(o) not in skip]

    @property
    def short_calls(self) -> list[OptionPos]:
        skip = self._spread_leg_ids
        return [o for o in self.options if o.kind == "CALL" and o.is_short and id(o) not in skip]

    @property
    def long_calls(self) -> list[OptionPos]:
        skip = self._spread_leg_ids
        return [o for o in self.options if o.kind == "CALL" and o.is_long and id(o) not in skip]


@dataclass
class Spread:
    """A vertical option spread — one long leg + one short leg, same underlying,
    same expiry, same type, different strikes. Risk is DEFINED (capped by the
    long leg), so it is measured by max loss, never the naked short notional.
    """
    symbol: str
    kind: Kind                 # "PUT" | "CALL"
    long_leg: OptionPos
    short_leg: OptionPos
    contracts: int             # matched leg size
    expiry: date

    def dte(self, today: date) -> int:
        return (self.expiry - today).days

    @property
    def width(self) -> float:
        return round(abs(self.long_leg.strike - self.short_leg.strike), 2)

    @property
    def net_per_share(self) -> float:
        """Long premium paid minus short premium received. >0 = net debit, <0 = net credit."""
        return round(self.long_leg.trade_price - self.short_leg.trade_price, 4)

    @property
    def is_debit(self) -> bool:
        return self.net_per_share > 0

    @property
    def net_cost(self) -> float:
        """Signed dollars: >0 paid (debit), <0 received (credit)."""
        return round(self.net_per_share * CONTRACT_MULTIPLIER * self.contracts, 2)

    @property
    def max_loss(self) -> float:
        """Capital genuinely at risk — the defined-risk number for this position."""
        d = abs(self.net_per_share)
        per_share = d if self.is_debit else (self.width - d)
        return round(per_share * CONTRACT_MULTIPLIER * self.contracts, 2)

    @property
    def max_profit(self) -> float:
        d = abs(self.net_per_share)
        per_share = (self.width - d) if self.is_debit else d
        return round(per_share * CONTRACT_MULTIPLIER * self.contracts, 2)

    @property
    def direction(self) -> str:
        """'bull' (profits if the underlying rises) or 'bear' (profits if it falls)."""
        if self.kind == "PUT":
            return "bear" if self.long_leg.strike > self.short_leg.strike else "bull"
        return "bull" if self.long_leg.strike < self.short_leg.strike else "bear"

    @property
    def label(self) -> str:
        return f"{self.direction.capitalize()} {self.kind.lower()} spread"

    @property
    def breakeven(self) -> float:
        d = self.net_per_share
        if self.kind == "PUT":
            # bear put (debit): long strike - debit ; bull put (credit): short strike - credit
            return round((self.long_leg.strike - d) if self.is_debit
                         else (self.short_leg.strike + d), 2)
        # calls: bull call (debit): long strike + debit ; bear call (credit): short strike + |credit|
        return round((self.long_leg.strike + d) if self.is_debit
                     else (self.short_leg.strike - d), 2)

    @property
    def mark_value(self) -> float:
        """Current dollar value of the spread (long mark - short mark)."""
        return round((self.long_leg.mark - self.short_leg.mark) * CONTRACT_MULTIPLIER * self.contracts, 2)

    @property
    def pl_open(self) -> float:
        return round(self.long_leg.pl_open + self.short_leg.pl_open, 2)


def detect_spreads(options: list[OptionPos]) -> tuple[list[Spread], set[int]]:
    """Find vertical spreads and return (spreads, set of leg ids that are in a spread).

    Conservative: a group of legs sharing (symbol, expiry, kind) is treated as a
    vertical only when it holds exactly one long and one short leg at different
    strikes — the unambiguous case. Anything more complex (calendars, ratios,
    butterflies, uneven leg counts) is left as naked legs, unchanged.
    """
    from collections import defaultdict
    groups: dict[tuple, list[OptionPos]] = defaultdict(list)
    for o in options:
        groups[(o.symbol, o.expiry, o.kind)].append(o)

    spreads: list[Spread] = []
    leg_ids: set[int] = set()
    for (sym, exp, kind), legs in groups.items():
        longs = [o for o in legs if o.is_long]
        shorts = [o for o in legs if o.is_short]
        if len(longs) == 1 and len(shorts) == 1 and longs[0].strike != shorts[0].strike:
            lo, so = longs[0], shorts[0]
            contracts = int(min(abs(lo.qty), abs(so.qty)))
            if contracts <= 0:
                continue
            spreads.append(Spread(sym, kind, lo, so, contracts, exp))
            leg_ids.add(id(lo))
            leg_ids.add(id(so))
    return spreads, leg_ids
