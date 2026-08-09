"""Screening universe — quality large-cap leaders by GICS sector.

A curated, defensible universe (not a gated constituent feed) so the screener is
reproducible and every name has deep Polygon financials. Deliberately biased to
established, profitable large-caps — the pool a buy-and-hold / wheel investor
would actually consider. Edit freely; the screener adapts to whatever is here.
"""
from __future__ import annotations

UNIVERSE: dict[str, list[str]] = {
    "Information Technology": ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD",
                               "ADBE", "CSCO", "ACN", "QCOM", "TXN", "INTU", "AMAT", "LRCX"],
    "Communication Services": ["GOOGL", "META", "NFLX", "DIS", "TMUS", "VZ", "CMCSA", "T"],
    "Consumer Discretionary": ["AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "BKNG", "TJX"],
    "Consumer Staples": ["PG", "KO", "PEP", "COST", "WMT", "PM", "MDLZ", "CL"],
    "Health Care": ["UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "AMGN", "ISRG"],
    "Financials": ["JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "SPGI", "BLK", "C"],
    "Industrials": ["CAT", "GE", "HON", "UNP", "RTX", "DE", "LMT", "UPS", "ETN", "BA"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX"],
    "Materials": ["LIN", "SHW", "APD", "FCX", "NEM", "ECL"],
    "Utilities": ["NEE", "DUK", "SO", "D", "AEP", "SRE"],
    "Real Estate": ["PLD", "AMT", "EQIX", "O", "SPG", "WELL"],
}


# Top ~50 US mega-caps by market value — a stable pre-warm set for AI briefs.
MEGACAP_50 = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "TSLA", "LLY", "JPM",
    "V", "WMT", "XOM", "MA", "UNH", "ORCL", "COST", "HD", "PG", "JNJ",
    "NFLX", "ABBV", "BAC", "KO", "CRM", "CVX", "MRK", "AMD", "PEP", "TMO",
    "LIN", "ADBE", "WFC", "ACN", "MCD", "CSCO", "ABT", "GE", "DHR", "QCOM",
    "TXN", "PM", "NOW", "INTU", "CAT", "VZ", "AXP", "ISRG", "AMGN", "IBM",
]


def all_symbols() -> list[str]:
    return [s for names in UNIVERSE.values() for s in names]


def sector_of(symbol: str) -> str | None:
    for sector, names in UNIVERSE.items():
        if symbol in names:
            return sector
    return None
