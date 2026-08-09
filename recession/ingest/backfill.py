"""S1 backfill — pull the CORE 25 to earliest available into observations.db,
log every fetch, and print a decade coverage matrix.

    python -m recession.ingest.backfill
"""
from __future__ import annotations

import pandas as pd

from recession import config
from recession.ingest import fred_client, price_client
from recession.registry import CORE_25
from recession.store import db


def run() -> None:
    if not config.FRED_API_KEY:
        raise SystemExit("FRED_API_KEY missing from .env")
    stats = []
    with db.connect() as conn:
        db.init_db(conn)
        for s in CORE_25:
            db.upsert_meta(conn, s)
            try:
                if s.source == "fred":
                    df, src = fred_client.get_observations(s.id), "fred"
                else:
                    df, src = price_client.get_observations(s.id, config.BACKTEST_START)
                n = db.upsert_observations(conn, s.id, df)
                db.log_fetch(conn, s.id, "ok" if n else "empty", n, src)
                first = str(df["obs_date"].min().date()) if n else "-"
                last = str(df["obs_date"].max().date()) if n else "-"
                stats.append((s.id, n, first, last, src))
                print(f"  {s.id:16} {n:>6} rows  {first} → {last}  [{src}]")
            except Exception as exc:
                db.log_fetch(conn, s.id, "error", 0, s.source, exc)
                stats.append((s.id, 0, "-", "-", "ERR"))
                print(f"  {s.id:16}  ERROR: {exc}")

    _coverage_matrix(stats)


def _coverage_matrix(stats) -> None:
    decades = list(range(1960, 2030, 10))
    hdr = "  ".join(f"{d}s" for d in decades)
    print(f"\nDecade coverage (series with data / 25):\n  decade  {hdr}")
    counts = {d: 0 for d in decades}
    with db.connect() as conn:
        for sid, n, *_ in stats:
            if not n:
                continue
            df = db.read_latest(conn, sid)
            if df.empty:
                continue
            yrs = df["obs_date"].dt.year
            for d in decades:
                if ((yrs >= d) & (yrs < d + 10)).any():
                    counts[d] += 1
    row = "  ".join(f"{counts[d]:>{len(str(d))+1}}" for d in decades)
    print(f"  count   {row}")
    total = sum(n for _, n, *_ in stats)
    ok = sum(1 for _, n, *_ in stats if n)
    print(f"\n{ok}/25 series populated · {total:,} total rows · db={config.DB_PATH}")


if __name__ == "__main__":
    run()
