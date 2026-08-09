"""Sector + beta reference and resolution.

Resolution priority (see `resolve`):
    sector: SECTOR_OVERRIDE  ->  live (Finnhub)  ->  static _REF  ->  "Unclassified"
    beta:   live (Finnhub)   ->  static _REF     ->  None

SECTOR_OVERRIDE is the owner's thematic taxonomy — it deliberately beats GICS /
Finnhub (which lumps IREN/CRWV under "Financials/Technology", etc.). LEVERAGED is
a RISK FLAG, never a sector: a 2x/3x product keeps its real sector (from the
override) and is additionally flagged.
"""
from __future__ import annotations

# beta >= this reads as "high beta" (flagged red in the UI).
HIGH_BETA = 1.5

# Owner thematic taxonomy — wins over Finnhub/GICS.
SECTOR_OVERRIDE = {
    # AI Infrastructure / Data Center (GICS misclassifies these)
    "IREN": "AI Infrastructure", "CRWV": "AI Infrastructure",
    "NBIS": "AI Infrastructure", "WULF": "AI Infrastructure", "CORZ": "AI Infrastructure",
    # Semiconductors
    "NVDA": "Semiconductors", "AVGO": "Semiconductors", "MRVL": "Semiconductors",
    "LRCX": "Semiconductors", "MU": "Semiconductors", "COHR": "Semiconductors",
    "CRDO": "Semiconductors", "AAOI": "Semiconductors", "SOXL": "Semiconductors",
    # Hyperscalers / Big Tech
    "AMZN": "Big Tech", "GOOGL": "Big Tech", "MSFT": "Big Tech",
    "META": "Big Tech", "AAPL": "Big Tech",
    # Fintech
    "HOOD": "Fintech", "SOFI": "Fintech",
    # Cybersecurity
    "CRWD": "Cybersecurity", "PANW": "Cybersecurity", "ZS": "Cybersecurity",
    "S": "Cybersecurity", "FTNT": "Cybersecurity", "NET": "Cybersecurity",
    "OKTA": "Cybersecurity", "CYBR": "Cybersecurity", "TENB": "Cybersecurity",
    "QLYS": "Cybersecurity", "CHKP": "Cybersecurity", "RBRK": "Cybersecurity",
    # Metals (uranium folds in here)
    "CCJ": "Metals", "CDE": "Metals", "UEC": "Metals", "DNN": "Metals",
    "URA": "Metals", "UUUU": "Metals", "GOLD": "Metals", "NEM": "Metals",
    "AEM": "Metals", "FCX": "Metals", "SCCO": "Metals", "MP": "Metals",
    "AA": "Metals", "GLD": "Metals", "SLV": "Metals",
    # Others
    "GLW": "Networking", "NOK": "Networking",
    "NFLX": "Streaming", "KTOS": "Aerospace/Defense",
    "BITX": "Crypto", "TQQQ": "Broad Index",
}

# 2x/3x products — a risk flag, NOT a sector.
LEVERAGED = {"SOXL", "BITX", "TQQQ"}

# Static fallback (sector, beta) — used when there is no override and no live feed.
_REF: dict[str, tuple[str, float]] = {
    "AAPL": ("Big Tech", 1.20), "MSFT": ("Big Tech", 0.90),
    "AMZN": ("Big Tech", 1.15), "GOOGL": ("Big Tech", 1.05), "META": ("Big Tech", 1.20),
    "NVDA": ("Semiconductors", 1.75), "AMD": ("Semiconductors", 1.70),
    "AVGO": ("Semiconductors", 1.10), "MRVL": ("Semiconductors", 1.60),
    "LRCX": ("Semiconductors", 1.55), "MU": ("Semiconductors", 1.40),
    "COHR": ("Semiconductors", 1.85), "CRDO": ("Semiconductors", 1.90),
    "AAOI": ("Semiconductors", 2.50), "SOXL": ("Semiconductors", 3.00),
    "CLS": ("Technology", 1.60), "NOW": ("Technology", 1.00),
    "PLTR": ("Technology", 2.60), "DRAM": ("Technology", 1.60), "NOK": ("Networking", 0.90),
    "GLW": ("Networking", 1.10), "NFLX": ("Streaming", 1.25),
    "HOOD": ("Fintech", 1.65), "SOFI": ("Fintech", 1.85),
    "CCJ": ("Metals", 1.10), "CDE": ("Metals", 1.30), "IREN": ("AI Infrastructure", 2.80),
    "CRWD": ("Cybersecurity", 1.15), "PANW": ("Cybersecurity", 1.10),
    "ZS": ("Cybersecurity", 1.20), "S": ("Cybersecurity", 1.60),
    "CRWV": ("AI Infrastructure", 2.40), "KTOS": ("Aerospace/Defense", 1.30),
    "BITX": ("Crypto", 3.50),
}


# Canonical GICS sectors, for diversification-gap notation.
BROAD_SECTORS = [
    "Technology", "Communication Services", "Financials", "Consumer Discretionary",
    "Consumer Staples", "Health Care", "Industrials", "Energy", "Materials",
    "Utilities", "Real Estate",
]

# Map the owner's thematic sectors onto broad GICS groups (None = not a GICS group).
THEMATIC_TO_BROAD = {
    "Semiconductors": "Technology", "Big Tech": "Technology",
    "AI Infrastructure": "Technology", "Technology": "Technology",
    "Networking": "Technology", "Electrical Equipment": "Industrials",
    "Fintech": "Financials", "Financials": "Financials",
    "Cybersecurity": "Technology",
    "Uranium": "Energy", "Energy": "Energy",
    "Metals": "Materials", "Mining": "Materials", "Materials": "Materials",
    "Streaming": "Communication Services", "Communication Services": "Communication Services",
    "Aerospace/Defense": "Industrials", "Industrials": "Industrials",
    "Consumer Discretionary": "Consumer Discretionary", "Retail": "Consumer Discretionary",
    "Consumer Staples": "Consumer Staples", "Health Care": "Health Care",
    "Healthcare": "Health Care", "Utilities": "Utilities", "Real Estate": "Real Estate",
    # Crypto / Broad Index deliberately map to None (not a GICS sector).
}


def broad_of(sector: str) -> str | None:
    """Broad GICS group for a (thematic) sector, or None if not GICS-classified."""
    return THEMATIC_TO_BROAD.get(sector)


def static_lookup(symbol: str) -> tuple[str, float | None]:
    """Static (sector, beta); ("Unclassified", None) if unknown."""
    return _REF.get(symbol.upper(), ("Unclassified", None))


def is_high_beta(beta: float | None) -> bool:
    return beta is not None and beta >= HIGH_BETA


def is_leveraged(symbol: str) -> bool:
    return symbol.upper() in LEVERAGED


def resolve(
    symbol: str,
    live_sector: str | None = None,
    live_beta: float | None = None,
) -> tuple[str, float | None, bool]:
    """Resolve (sector, beta, leveraged) applying override/live/static priority."""
    sym = symbol.upper()
    stat_sector, stat_beta = static_lookup(sym)
    sector = SECTOR_OVERRIDE.get(sym) or live_sector or stat_sector or "Unclassified"
    beta = live_beta if live_beta is not None else stat_beta
    return sector, beta, is_leveraged(sym)
