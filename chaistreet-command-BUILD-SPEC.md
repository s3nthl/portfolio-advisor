# ChaiStreet Command — Build Spec for Claude Code

You are the developer. This is the architect's spec. Build it agentically,
one phase at a time, stopping at each **GATE** for the user to verify before
continuing. Do not build ahead of the gates.

## Context
Senthil runs a ~$1.1M Schwab options-wheel account he is winding down toward
buy-and-hold. Today he uploads a CSV statement and reasons about it by hand.
This app replaces that loop: pull the live book **on demand**, compute a fixed
methodology, render a dashboard.

## Non-negotiable constraints
- **Read-only.** No order placement, ever. OAuth read scope only.
- **On-demand pull.** A "Refresh" button triggers a fetch. NO scheduler, NO polling.
- **Local-first.** Runs on his machine. Secrets local (`.env`, gitignored).
- **Build in `/Users/sandbox.ai/myport`.** This is the target directory.
  Structure it so it can later be lifted into the ChaiStreet monorepo as a
  self-contained module (keep it importable, no hard-coded absolute paths).
- Stack: **Python 3.11+, FastAPI, schwab-py, SQLite, plain HTML/JS frontend**
  (React only if he asks — v1 doesn't need it).

## The locked methodology — this IS the product, get it exact
- CSP exposure measured by **NOTIONAL** = strike x 100 x abs(qty). NEVER option mark.
- Real cash = Cash & Sweep + any money-market holdings (e.g. SWVXX) **if present**.
  SWVXX is often $0 or absent — sum whatever money-market balances exist, default
  to zero. Do NOT hard-code SWVXX as a required field. Option Buying Power and
  Intraday Buying Power are **NOT** cash — never sum them into cash.
- Keep P&L in **separate universes**: stock / CSP / LEAP / covered-call. Never blend.
- **LEAP** = a long call with > 182 days to expiration. (<=182 = "other long".)
- **Covered calls never written below cost basis** (enforce in any CC helper).
- **Assignment waterfall**: order short puts by expiry; assume each assigns;
  drain real cash first; a margin loan begins only when cash reaches 0.
- **VXN posture** (placeholder feed for now, real bands):
  <20 risk-on 5-10% cash / 20-25 caution 10-15% / 25-30 fear 15-25% /
  30-35 elevated 25-35% / >35 panic: deploy down to ~10%.

## Architecture — 4 layers, one data shape
Everything downstream sees a normalized `Book`. The data SOURCE (fixture vs
live Schwab) is swappable behind one function; nothing else changes.

```
ingest/     source -> normalized Book.
              - load_book_from_schwab()  <- THE runtime source. Live, read-only.
              - inline fixture / last-snapshot loader  <- test + fallback ONLY.
analytics/  pure functions over Book: buckets(), assignment_waterfall(),
              margin_snapshot(), vxn_posture(). No I/O. Unit-testable.
store/      SQLite: one snapshot row per refresh -> day-over-day deltas.
api/        FastAPI: GET /api/refresh (on-demand pull), serves the UI.
web/        dashboard + Refresh button (hits /api/refresh).
tests/      regression tests against known statement numbers (below).
```

### Normalized types
`StockPos(symbol, qty, cost_basis, mark)` with `market_value`, `pl_open`.
`OptionPos(symbol, kind PUT|CALL, qty (+long/-short), strike, expiry, trade_price, pl_open)`
with `dte(today)`, `notional`, `buyback_cost()`.
`Balances(cash_and_sweep, option_buying_power, net_liq, margin_loan=0)`.
`Book(stocks[], options[], balances, as_of)`.

## Build phases — STOP at each GATE

**Phase 0 — Scaffold.** New dir, venv, deps, folders above, `.env.example`,
`.gitignore` (must ignore `.env`, token file, `*.db`). `/health` endpoint.
**GATE:** `GET /health` returns ok.

**Phase 1 — Schwab read-only auth + fetch.** Use schwab-py
(`client_from_login_flow`, token file). Fetch positions, balances, option marks.
Dump raw JSON to `sample_book.json`. **Do NOT ask the user for a CSV statement.**
The only credentials needed are the user's Schwab app key/secret/callback, which
the USER places in `.env` themselves and runs the browser OAuth flow locally —
never paste creds into chat. Also build the offline fixture loader (see Phase 2)
so the app runs without a live connection.
**GATE:** his real book prints to console; fixture loads offline.

**Phase 2 — Analytics engine.** Implement the methodology above as pure
functions. **Do NOT request a CSV file.** Build the regression test against a
small INLINE fixture using these KNOWN Jul 31 outputs as hard-coded expected
values:
```
Stock MV     $863,056  (72.4% of gross)
Cash         $239,898  (21.8% of Net Liq)
LEAP MV      $89,344
CSP notional $496,800  (14 legs)
Net Liq      $1,099,690
Waterfall peak loan (all assign)  $256,902
VXN 28.6 -> band "fear", cash 21.8% -> "in range" (target 15-25%)
```
**GATE:** engine reproduces every number above exactly.

**Phase 3 — Snapshot store.** SQLite schema; write one row per refresh;
a query for day-over-day deltas.
**GATE:** two refreshes -> queryable history.

**Phase 4 — Dashboard.** On-demand Refresh button -> /api/refresh. Render the
Command board: 4 buckets (stock / CSP / covered-call / LEAP), VXN posture,
assignment-waterfall table, margin summary. Dark theme, matches his HTML boards.
**GATE:** live book renders; Refresh re-pulls on click.

**Phase 5 — (hold) VXN feed + sell/keep flags.** Leave VXN as a stubbed
function returning a fixed value behind a clean interface. Do not wire a feed
yet. Sell/keep catalyst-sort and covered-call delta planner come later.

## Data source of truth — READ THIS
Live Schwab is the **sole runtime source**. The user will never download or
upload a CSV statement again — do NOT build any CSV-upload or file-import
feature in the UI.

Why live beats CSV (this is the whole reason the app exists): the CSV is a
stale, internally-inconsistent snapshot — option marks are missing or old,
assignments settle on a lag so positions and cash disagree across two
timestamps in the same file. The Schwab API returns the same live data the
thinkorswim screen shows: real-time marks, real-time P&L, settled position
state, one consistent cash figure at one timestamp. The engine must consume
**live marks and settled state** from the API, not reconstruct them.

The offline fixture survives for exactly two non-runtime reasons:
  1. **Offline regression test** — a small INLINE fixture (hard-coded Book with
     the Jul 31 known-answer numbers) for Phase 2. Do not ask the user for or
     import a CSV file; the numbers in this spec are sufficient. Tests can't
     depend on a live account.
  2. **Degraded-mode fallback** — if the API is down or the token expires
     mid-session, the app can load the last saved `sample_book.json` snapshot
     instead of showing nothing.

Never treat the fixture and API as equivalent sources. API = truth.
Fixture = test/fallback. There is NO CSV-upload feature anywhere in this app.

## Going-live seam
`load_book_from_schwab()` is the single live-wiring point. It must return the
same `Book` shape as the fixture loader. Map Schwab account JSON into
StockPos/OptionPos/Balances there. A `CHAI_SOURCE=fixture|schwab` env var
selects the source. Until wired, the whole app works on the fixture.

## Deliverable order
Phase 0 -> GATE -> 1 -> GATE -> 2 -> GATE -> ... Present each gate's result and
wait. Do not build the whole thing in one shot.
