"""Tidy observation store (spec §2.2). Long/tidy rows, not JSON blobs, so
cross-series scoring / backtest / analog matching are possible.

Phase 0 stores the LATEST (revised) snapshot under a sentinel vintage_date; true
ALFRED vintages are layered in at S3 for the point-in-time backtest. Non-lookahead
in S2 is enforced by filtering on obs_date, not vintage.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pandas as pd

from recession import config

LATEST_VINTAGE = "9999-12-31"   # sentinel = "latest revised snapshot"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    series_id    TEXT NOT NULL,
    obs_date     TEXT NOT NULL,
    value        REAL,
    vintage_date TEXT NOT NULL,
    PRIMARY KEY (series_id, obs_date, vintage_date)
);
CREATE INDEX IF NOT EXISTS idx_obs_series_date ON observations(series_id, obs_date);
CREATE INDEX IF NOT EXISTS idx_obs_vintage ON observations(series_id, vintage_date);

CREATE TABLE IF NOT EXISTS series_meta (
    series_id TEXT PRIMARY KEY,
    display_name TEXT, section TEXT, subsector TEXT,
    source TEXT, native_freq TEXT, transform TEXT,
    direction TEXT, weight REAL, is_core INTEGER,
    lag_days INTEGER, fallback_source TEXT, notes TEXT
);

CREATE TABLE IF NOT EXISTS fetch_log (
    series_id TEXT, fetched_at TEXT, status TEXT,
    rows INTEGER, source_used TEXT, error TEXT
);
"""


@contextmanager
def connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def upsert_observations(conn, series_id: str, df: pd.DataFrame,
                        vintage_date: str = LATEST_VINTAGE) -> int:
    """df must have columns obs_date (str/date) and value (float|NaN)."""
    if df is None or df.empty:
        return 0
    rows = [(series_id, str(pd.Timestamp(d).date()),
             (None if pd.isna(v) else float(v)), vintage_date)
            for d, v in zip(df["obs_date"], df["value"])]
    conn.executemany(
        "INSERT OR REPLACE INTO observations(series_id,obs_date,value,vintage_date)"
        " VALUES (?,?,?,?)", rows)
    return len(rows)


def upsert_meta(conn, s) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO series_meta(series_id,display_name,section,subsector,"
        "source,native_freq,transform,direction,weight,is_core,lag_days,fallback_source,notes)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (s.id, s.name, s.section, s.subsector, s.source, s.native_freq, s.transform,
         s.direction, s.weight, 1, s.lag_days, s.fallback, s.notes))


def log_fetch(conn, series_id, status, rows, source_used, error="") -> None:
    conn.execute(
        "INSERT INTO fetch_log(series_id,fetched_at,status,rows,source_used,error)"
        " VALUES (?,datetime('now'),?,?,?,?)",
        (series_id, status, int(rows), source_used, str(error)[:300]))


def read_latest(conn, series_id: str) -> pd.DataFrame:
    """Latest revised series as (obs_date[datetime], value), sorted."""
    df = pd.read_sql_query(
        "SELECT obs_date, value FROM observations "
        "WHERE series_id=? AND vintage_date=? ORDER BY obs_date",
        conn, params=(series_id, LATEST_VINTAGE))
    if not df.empty:
        df["obs_date"] = pd.to_datetime(df["obs_date"])
    return df


def series_span(conn, series_id: str):
    r = conn.execute(
        "SELECT COUNT(*), MIN(obs_date), MAX(obs_date) FROM observations "
        "WHERE series_id=? AND vintage_date=?", (series_id, LATEST_VINTAGE)).fetchone()
    return r  # (rows, first, last)
