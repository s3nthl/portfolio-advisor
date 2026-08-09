# s3nthl portfolio dashboard

On-demand, read-only dashboard for a Schwab options-wheel account. Pull the live
book on a button press, compute a fixed methodology, render a command board.
(Internally the module is still `chai_*` / ChaiStreet Command — only the UI is
branded "s3nthl portfolio dashboard".)

**Read-only. No order placement, ever. On-demand pull only — no scheduler.**

## Layout

```
ingest/     data source -> normalized Book (Schwab live | CSV/fixture)
analytics/  pure functions over Book (buckets, waterfall, margin, VXN posture)
store/      SQLite: one snapshot row per refresh -> day-over-day deltas
api/        FastAPI: /api/refresh (on-demand), serves the UI
web/        dashboard + Refresh button
tests/      regression tests against known statement numbers
config.py   env-driven config (portable, no absolute paths)
```

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in Schwab creds when ready

# run the server
python -m api.app               # or: uvicorn api.app:app
curl http://127.0.0.1:8000/health
```

## Data source

`CHAI_SOURCE=fixture` (default) runs fully offline. `CHAI_SOURCE=schwab` pulls
the live book (read-only). Live Schwab is the sole runtime truth; the fixture
loader exists only for offline regression tests and degraded-mode fallback.

## Run the dashboard

```bash
CHAI_SOURCE=schwab python -m uvicorn api.app:app --port 8000
# open http://127.0.0.1:8000  (Refresh button = on-demand live pull)
```

Tabs: **Portfolio %** (per-position allocation), **Sector** (all positions by
sector + high-beta flags), **Command** (4 buckets, paired VXN/VIX posture, alerts,
margin), **Waterfall** (assignment cascade), **History** (snapshots + day-over-day
deltas). No scheduler — a snapshot is written only when you click Refresh.

**LEAPs/Calls** = long call with > 90 DTE (owner override of the 182-day spec).
**Sector/beta** come from a curated static map (`analytics/reference.py`) behind a
`lookup()` seam — Schwab supplies neither; a Finnhub feed can replace it (Phase 5).

## Print the book

```bash
python -m ingest                        # offline fixture (default)
CHAI_SOURCE=schwab python -m ingest      # live read-only pull (needs .env creds)
```

The first live run opens a browser OAuth flow (schwab-py), caches a token, and
dumps the raw account JSON to `sample_book.json` (gitignored). Later runs reuse
the token. If a live pull fails, the app falls back to the last snapshot.

## Build status

- [x] **Phase 0** — scaffold, deps, `/health`
- [x] **Phase 1** — Schwab read-only seam + inline fixture; `python -m ingest` prints the book
- [x] **Phase 2** — analytics engine (buckets / waterfall / margin / VXN posture), 20 regression tests reproduce Jul-31 exactly
- [x] **Phase 3** — SQLite snapshot store; one row per refresh + day-over-day deltas
- [x] **Phase 4** — dashboard: `/api/refresh` on-demand pull, tabbed dark-theme command board
- [ ] Phase 5 (hold) — real VXN/VIX feed + sell/keep flags + covered-call delta planner
