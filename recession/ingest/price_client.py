"""Price client — yfinance primary, stooq CSV fallback (spec §2.1: never let a
yfinance series be the sole source). Same interface as fred_client.
"""
from __future__ import annotations

import io

import httpx
import pandas as pd

# yfinance ticker -> stooq symbol
_STOOQ = {"^GSPC": "^spx", "^RUT": "^rut", "^DJT": "^dji", "^N225": "^nkx", "^SOX": "^sox"}


def _from_yfinance(ticker: str, start: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(ticker, start=start, progress=False, auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame(columns=["obs_date", "value"])
    close = df["Close"]
    if isinstance(close, pd.DataFrame):      # MultiIndex columns for single ticker
        close = close.iloc[:, 0]
    out = close.reset_index()
    out.columns = ["obs_date", "value"]
    out["obs_date"] = pd.to_datetime(out["obs_date"])
    return out.dropna()


def _from_stooq(ticker: str, start: str) -> pd.DataFrame:
    sym = _STOOQ.get(ticker, ticker.lower().lstrip("^"))
    r = httpx.get(f"https://stooq.com/q/d/l/?s={sym}&i=d", timeout=60.0)
    txt = r.text.strip()
    if not txt or txt.lower().startswith("<") or "no data" in txt.lower():
        return pd.DataFrame(columns=["obs_date", "value"])
    df = pd.read_csv(io.StringIO(txt))
    if "Close" not in df.columns:
        return pd.DataFrame(columns=["obs_date", "value"])
    out = df[["Date", "Close"]].rename(columns={"Date": "obs_date", "Close": "value"})
    out["obs_date"] = pd.to_datetime(out["obs_date"])
    out = out[out["obs_date"] >= pd.Timestamp(start)]
    return out.dropna()


def get_observations(ticker: str, start: str) -> tuple[pd.DataFrame, str]:
    """Returns (df, source_used). Tries yfinance, falls back to stooq."""
    try:
        df = _from_yfinance(ticker, start)
        if not df.empty:
            return df, "yfinance"
    except Exception:
        pass
    try:
        df = _from_stooq(ticker, start)
        if not df.empty:
            return df, "stooq"
    except Exception:
        pass
    return pd.DataFrame(columns=["obs_date", "value"]), "none"
