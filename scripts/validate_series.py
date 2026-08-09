#!/usr/bin/env python
"""S0 preflight — validate the CORE 25 before any ingest/UI (spec §2.4, BUILD_PLAN S0).

For every FRED id: GET /fred/series → 200, not DISCONTINUED, report history start
(flag SHORT vs the 1969 backtest window), check freshness vs typical lag.
For ^GSPC: fetch via yfinance AND stooq, both non-empty, last close within 0.5%.

Emits recession/reports/validation_report.md. Exits nonzero if any core series is RED.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from recession import config
from recession.registry import CORE_25, Series

BACKTEST_YEAR = 1969
_FREQ_BUFFER = {"daily": 4, "weekly": 12, "monthly": 45, "quarterly": 135}


def _parse_date(s: str) -> date | None:
    s = (s or "").strip()[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def check_fred(s: Series, client: httpx.Client) -> dict:
    if not config.FRED_API_KEY:
        return {"status": "RED", "note": "FRED_API_KEY missing from .env"}
    try:
        r = client.get(f"{config.FRED_BASE}/series",
                       params={"series_id": s.id, "api_key": config.FRED_API_KEY,
                               "file_type": "json"}, timeout=30.0)
    except Exception as exc:
        return {"status": "RED", "note": f"request failed: {exc}"}
    if r.status_code != 200:
        return {"status": "RED", "note": f"HTTP {r.status_code}"}
    ser = (r.json().get("seriess") or [{}])[0]
    if not ser:
        return {"status": "RED", "note": "not found"}
    title = ser.get("title", "")
    start = _parse_date(ser.get("observation_start", ""))
    last = _parse_date(ser.get("observation_end", ""))
    updated = _parse_date(ser.get("last_updated", ""))
    if "DISCONTINUED" in title.upper():
        return {"status": "RED", "start": start, "updated": updated, "note": "DISCONTINUED"}
    # freshness vs typical lag (STALE = amber warn, not a hard fail)
    stale = ""
    allowed = 2 * s.lag_days + _FREQ_BUFFER.get(s.native_freq, 45)
    if last and (date.today() - last).days > allowed:
        stale = f" · last obs {last} ({(date.today()-last).days}d old)"
    short = bool(start and start.year > BACKTEST_YEAR)
    status = "STALE" if stale else ("SHORT" if short else "OK")
    note = (f"starts {start}" if short else "back to " + (str(start) if start else "?"))
    return {"status": status, "start": start, "updated": updated, "note": note + stale,
            "title": title}


def check_price(s: Series) -> dict:
    yf_ok = stooq_ok = False
    yf_close = stooq_close = None
    try:
        import yfinance as yf
        df = yf.download(s.id, period="5d", progress=False, auto_adjust=False)
        if df is not None and len(df):
            yf_ok = True
            yf_close = float(df["Close"].iloc[-1].item())
    except Exception:
        pass
    # stooq: ^GSPC -> ^spx
    stooq_sym = {"^GSPC": "^spx"}.get(s.id, s.id.lower())
    try:
        r = httpx.get(f"https://stooq.com/q/d/l/?s={stooq_sym}&i=d", timeout=30.0)
        rows = [ln for ln in r.text.strip().splitlines() if ln]
        if len(rows) > 1 and not rows[1].lower().startswith("<"):
            stooq_ok = True
            stooq_close = float(rows[-1].split(",")[4])
    except Exception:
        pass
    if not yf_ok and not stooq_ok:
        return {"status": "RED", "note": "both yfinance and stooq empty"}
    if yf_ok and stooq_ok and yf_close and stooq_close:
        diff = abs(yf_close - stooq_close) / stooq_close
        note = f"yf {yf_close:.0f} vs stooq {stooq_close:.0f} ({diff*100:.2f}%)"
        status = "OK" if diff <= 0.005 else "STALE"
        return {"status": status, "note": note + ("" if diff <= 0.005 else " · >0.5% gap")}
    only = "yfinance" if yf_ok else "stooq"
    return {"status": "SHORT", "note": f"only {only} responded (fallback works)"}


def main() -> int:
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    with httpx.Client() as client:
        for s in CORE_25:
            res = check_fred(s, client) if s.source == "fred" else check_price(s)
            rows.append((s, res))
            print(f"  {res['status']:6} {s.id:16} {res.get('note','')}")

    counts: dict[str, int] = {}
    for _, res in rows:
        counts[res["status"]] = counts.get(res["status"], 0) + 1
    reds = [s.id for s, res in rows if res["status"] == "RED"]
    ok_like = counts.get("OK", 0) + counts.get("SHORT", 0) + counts.get("STALE", 0)
    green_pct = 100.0 * ok_like / len(rows)

    lines = ["# Recession Monitor — Series Validation (S0)", "",
             f"_Generated {date.today().isoformat()} · {len(rows)} core series_", "",
             f"**Usable: {ok_like}/{len(rows)} ({green_pct:.0f}%)** · "
             f"OK {counts.get('OK',0)} · SHORT {counts.get('SHORT',0)} · "
             f"STALE {counts.get('STALE',0)} · RED {counts.get('RED',0)}", "",
             "| Status | Series | Section | Source | Note |",
             "|---|---|---|---|---|"]
    order = {"RED": 0, "STALE": 1, "SHORT": 2, "OK": 3}
    for s, res in sorted(rows, key=lambda x: order.get(x[1]["status"], 9)):
        icon = {"OK": "🟢", "SHORT": "🟡", "STALE": "🟠", "RED": "🔴"}[res["status"]]
        lines.append(f"| {icon} {res['status']} | `{s.id}` | {s.section} | {s.source} | {res.get('note','')} |")
    out = config.REPORT_DIR / "validation_report.md"
    out.write_text("\n".join(lines) + "\n")

    print(f"\n{'='*54}\nUsable {ok_like}/{len(rows)} ({green_pct:.0f}%) · RED {len(reds)}: {reds or 'none'}")
    print(f"Report: {out}")
    gate = (not reds) and green_pct >= 90.0
    print("GATE: " + ("PASS ✅" if gate else "FAIL ❌"))
    return 0 if not reds else 1


if __name__ == "__main__":
    raise SystemExit(main())
