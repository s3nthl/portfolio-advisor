"""FRED + ALFRED client. Latest observations for S1/S2; true point-in-time
vintages (ALFRED realtime periods) for the S3 backtest. Retry with backoff on
429/5xx. Never raises past the retry budget for a soft failure — returns empty.
"""
from __future__ import annotations

import time

import httpx
import pandas as pd

from recession import config

_OBS = f"{config.FRED_BASE}/series/observations"
_MAX_TRIES = 4


def _get(params: dict) -> dict:
    params = {**params, "api_key": config.FRED_API_KEY, "file_type": "json"}
    last = None
    for attempt in range(_MAX_TRIES):
        try:
            r = httpx.get(_OBS, params=params, timeout=60.0)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(1.5 * (2 ** attempt))
                continue
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:150]}")
        except httpx.HTTPError as exc:
            last = str(exc)
            time.sleep(1.5 * (2 ** attempt))
    raise RuntimeError(f"FRED fetch failed after {_MAX_TRIES} tries: {last}")


def _to_frame(obs: list[dict]) -> pd.DataFrame:
    if not obs:
        return pd.DataFrame(columns=["obs_date", "value"])
    df = pd.DataFrame(obs)
    df = df[["date", "value"]].rename(columns={"date": "obs_date"})
    df["value"] = pd.to_numeric(df["value"].replace(".", pd.NA), errors="coerce")
    df["obs_date"] = pd.to_datetime(df["obs_date"])
    return df


def get_observations(series_id: str, start: str = config.BACKTEST_START) -> pd.DataFrame:
    """Latest (revised) full history from `start`."""
    data = _get({"series_id": series_id, "observation_start": start})
    return _to_frame(data.get("observations", []))


def get_vintage(series_id: str, as_of: str,
                start: str = config.BACKTEST_START) -> pd.DataFrame:
    """Series as it was known ON `as_of` (ALFRED realtime). No lookahead: values
    published after `as_of` are invisible. Used by the S3 point-in-time backtest."""
    data = _get({"series_id": series_id, "observation_start": start,
                 "realtime_start": as_of, "realtime_end": as_of})
    return _to_frame(data.get("observations", []))
