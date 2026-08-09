"""SQLite snapshot store — one row per refresh, plus day-over-day deltas.

Each refresh persists the engine's `summary()` as a row: scalar headline
columns for fast delta queries, plus the full JSON payload for detail. No
business logic here — the numbers come from analytics; this layer only stores
and diffs them.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import config
from analytics import summary
from ingest.models import Book

# Scalar columns pulled out of summary() for cheap querying/deltas.
_SCALAR_COLUMNS = [
    "net_liq", "gross_assets", "stock_mv", "stock_pct_gross",
    "real_cash", "cash_pct_netliq", "csp_notional", "dry_powder",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    refreshed_at           TEXT    NOT NULL,   -- ISO timestamp of this refresh
    as_of                  TEXT    NOT NULL,   -- book.as_of (date the data is for)
    source                 TEXT    NOT NULL,   -- fixture | schwab
    net_liq                REAL,
    gross_assets           REAL,
    stock_mv               REAL,
    stock_pct_gross        REAL,
    real_cash              REAL,
    cash_pct_netliq        REAL,
    dry_powder             REAL,
    csp_notional           REAL,
    csp_count              INTEGER,
    covered_call_notional  REAL,
    covered_call_count     INTEGER,
    leap_mv                REAL,
    leap_count             INTEGER,
    margin_loan            REAL,
    peak_loan_if_assigned  REAL,
    option_buying_power    REAL,
    vxn                    REAL,
    vxn_band               TEXT,
    cash_status            TEXT,
    cc_violations          INTEGER,
    payload                TEXT    NOT NULL     -- full summary() JSON
);
CREATE INDEX IF NOT EXISTS idx_snapshots_refreshed_at ON snapshots(refreshed_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_as_of ON snapshots(as_of);
"""


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or config.CHAI_DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | None = None) -> None:
    """Create the schema if it doesn't exist; migrate columns added later."""
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        # lightweight migrations for columns added after a DB already existed
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(snapshots)")}
        for col in ("dry_powder",):
            if col not in existing:
                conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} REAL")


@dataclass
class Snapshot:
    id: int
    refreshed_at: str
    as_of: str
    source: str
    metrics: dict          # the scalar columns
    payload: dict          # full summary() JSON

    def __getitem__(self, key):  # convenience for delta code
        return self.metrics[key]


def _row_to_snapshot(row: sqlite3.Row) -> Snapshot:
    d = dict(row)
    payload = json.loads(d.pop("payload"))
    meta = {k: d.pop(k) for k in ("id", "refreshed_at", "as_of", "source")}
    return Snapshot(metrics=d, payload=payload, **meta)


def write_snapshot(
    book: Book,
    source: str | None = None,
    vxn: float | None = None,
    refreshed_at: str | None = None,
    db_path: Path | None = None,
) -> int:
    """Compute the engine summary for `book` and persist it. Returns the row id."""
    init_db(db_path)
    s = summary(book, vxn=vxn)
    ts = refreshed_at or datetime.now().isoformat(timespec="seconds")

    m = s["margin"]
    p = s["posture"]
    b = s["buckets"]
    values = {
        "refreshed_at": ts,
        "as_of": s["as_of"],
        "source": (source or config.CHAI_SOURCE),
        "net_liq": s["net_liq"],
        "gross_assets": s["gross_assets"],
        "stock_mv": s["stock_mv"],
        "stock_pct_gross": s["stock_pct_gross"],
        "real_cash": s["real_cash"],
        "cash_pct_netliq": s["cash_pct_netliq"],
        "dry_powder": s.get("dry_powder"),
        "csp_notional": b["csp"]["notional"],
        "csp_count": b["csp"]["count"],
        "covered_call_notional": b["covered_call"]["notional"],
        "covered_call_count": b["covered_call"]["count"],
        "leap_mv": b["leap"]["market_value"],
        "leap_count": b["leap"]["count"],
        "margin_loan": m["margin_loan"],
        "peak_loan_if_assigned": m["loan_if_all_assigned"],
        "option_buying_power": m["option_buying_power"],
        "vxn": p["value"],
        "vxn_band": p["band"],
        "cash_status": p["status"],
        "cc_violations": len(s["covered_call_violations"]),
        "payload": json.dumps(s),
    }
    cols = ", ".join(values)
    placeholders = ", ".join(f":{c}" for c in values)
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO snapshots ({cols}) VALUES ({placeholders})", values
        )
        return cur.lastrowid


def latest_snapshots(n: int = 10, db_path: Path | None = None) -> list[Snapshot]:
    """Return the most recent `n` snapshots, newest first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM snapshots ORDER BY refreshed_at DESC, id DESC LIMIT ?",
            (n,),
        ).fetchall()
    return [_row_to_snapshot(r) for r in rows]


def count_snapshots(db_path: Path | None = None) -> int:
    with _connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]


def prior_day_net_liq(today: str, db_path: Path | None = None) -> tuple[float, str] | None:
    """Net liq (and timestamp) of the latest snapshot from a calendar day BEFORE
    `today` (YYYY-MM-DD) — a prior-close proxy for a midnight-resetting day P/L.
    Returns None if there is no earlier-day snapshot yet.
    """
    init_db(db_path)  # table may not exist yet on the very first refresh
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT net_liq, refreshed_at FROM snapshots "
            "WHERE date(refreshed_at) < ? ORDER BY refreshed_at DESC LIMIT 1",
            (today,),
        ).fetchone()
    return (row["net_liq"], row["refreshed_at"]) if row else None


@dataclass
class Delta:
    metric: str
    previous: float
    current: float
    change: float
    pct_change: float | None


def _delta(metric: str, prev: float, curr: float) -> Delta:
    prev = float(prev or 0.0)
    curr = float(curr or 0.0)
    change = round(curr - prev, 2)
    pct = round((change / prev) * 100, 2) if prev else None
    return Delta(metric, prev, curr, change, pct)


def latest_delta(db_path: Path | None = None) -> dict:
    """Day-over-day delta between the two most recent snapshots.

    Returns {"current":..., "previous":..., "deltas":{metric: Delta}} or a
    {"error":...} shape if fewer than two snapshots exist.
    """
    snaps = latest_snapshots(2, db_path=db_path)
    if len(snaps) < 2:
        return {"error": "need at least two snapshots for a delta",
                "count": len(snaps)}
    curr, prev = snaps[0], snaps[1]
    deltas = {m: _delta(m, prev.metrics[m], curr.metrics[m]) for m in _SCALAR_COLUMNS}
    return {
        "current": {"id": curr.id, "refreshed_at": curr.refreshed_at, "as_of": curr.as_of},
        "previous": {"id": prev.id, "refreshed_at": prev.refreshed_at, "as_of": prev.as_of},
        "deltas": deltas,
    }
