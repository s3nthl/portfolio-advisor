# CLAUDE.md — ChaiStreet Command

Instructions for the developer (Claude Code) working in this repo. Read this
every session before writing code. The full build spec lives alongside this
file: `chaistreet-command-BUILD-SPEC.md`. This file is the condensed,
always-on version — when the two agree, follow either; the spec has more detail.

## What this is
A local-first, **read-only** dashboard over a Schwab options-wheel account.
Pulls the live book **on demand** (a Refresh button), computes a fixed
methodology, renders a "Command board." Replaces a manual CSV-statement
workflow — the user will never upload a statement again.

## Hard constraints (do not violate)
- **Read-only.** OAuth read scope only. NEVER build order placement or any
  write/trade endpoint.
- **On-demand pull only.** A Refresh button triggers a fetch. NO scheduler,
  NO background polling.
- **Live Schwab is the sole runtime data source.** NO CSV-upload feature
  anywhere in the UI. Do not ask the user for a statement file.
- **Local-first.** Secrets in `.env` (gitignored). Token file gitignored.
  User places their own Schwab app key/secret in `.env` and runs OAuth
  locally — never request creds in chat.
- **Build here:** this repo is `/Users/sandbox.ai/myport`. Keep it importable
  and path-independent so it can later be lifted into the ChaiStreet monorepo
  as a self-contained module.

## Stack
Python 3.11+, FastAPI, schwab-py, SQLite, plain HTML/JS frontend.
(No React unless the user asks.)

## The methodology — this IS the product. Get every number exact.
- **CSP exposure = NOTIONAL** = strike x 100 x abs(qty). NEVER the option mark.
- **Real cash** = Cash & Sweep + any money-market holdings (e.g. SWVXX) **if
  present** — SWVXX is often $0 or absent, so sum whatever exists, default 0.
  Do NOT hard-code SWVXX. Option Buying Power and Intraday Buying Power are
  **NOT** cash — never add them to cash.
- **Separate P&L universes:** stock / CSP / LEAP / covered-call. NEVER blend.
- **LEAP** = long call with > 182 DTE. (<= 182 = "other long".)
- **Covered calls never written below cost basis** (enforce in any CC helper).
- **Assignment waterfall:** order short puts by expiry; assume each assigns;
  drain real cash first; a margin loan starts only when cash reaches 0.
- **VXN posture** (placeholder feed, real bands): <20 risk-on 5-10% cash /
  20-25 caution 10-15% / 25-30 fear 15-25% / 30-35 elevated 25-35% /
  >35 panic (deploy down to ~10%).

## Architecture — one data shape, swappable source
Everything downstream sees a normalized `Book`. The source (live Schwab vs
offline fixture) hides behind one function; nothing else changes.

```
ingest/     load_book_from_schwab()   <- runtime source, live read-only
            inline fixture / snapshot  <- test + degraded fallback ONLY
analytics/  pure functions: buckets(), assignment_waterfall(),
            margin_snapshot(), vxn_posture(). No I/O. Unit-testable.
store/      SQLite: one snapshot row per refresh -> day-over-day deltas
api/        FastAPI: GET /api/refresh (on-demand), serves the UI
web/        Command board + Refresh button
tests/      regression tests vs the known numbers below
```

### Normalized types
- `StockPos(symbol, qty, cost_basis, mark)` -> `market_value`, `pl_open`
- `OptionPos(symbol, kind PUT|CALL, qty +long/-short, strike, expiry,
  trade_price, pl_open)` -> `dte(today)`, `notional`, `buyback_cost()`
- `Balances(cash_and_sweep, option_buying_power, net_liq, margin_loan=0)`
- `Book(stocks[], options[], balances, as_of)`

## Build in phases — STOP at each GATE, let the user verify
- **P0 Scaffold:** dirs, venv, deps, `.env.example`, `.gitignore`
  (ignore `.env`, token, `*.db`), `/health`. GATE: /health ok.
- **P1 Schwab auth+fetch:** schwab-py login flow, fetch positions/balances/
  marks, dump `sample_book.json`. Do NOT ask for a CSV. GATE: real book prints.
- **P2 Analytics:** methodology as pure functions. Regression test against an
  INLINE fixture with these known Jul 31 values (do NOT import a CSV):
  ```
  Stock MV     $863,056  (72.4% of gross)
  Cash         $239,898  (21.8% of Net Liq)
  LEAP MV      $89,344
  CSP notional $496,800  (14 legs)
  Net Liq      $1,099,690
  Waterfall peak loan (all assign)  $256,902
  VXN 28.6 -> "fear", cash 21.8% -> "in range" (target 15-25%)
  ```
  GATE: engine reproduces every number exactly.
- **P3 Store:** SQLite snapshots + delta query. GATE: two refreshes -> history.
- **P4 Dashboard:** Refresh -> /api/refresh. Render 4 buckets, VXN posture,
  waterfall table, margin summary. Dark theme. GATE: live book renders.
- **P5 (hold):** leave VXN a stubbed function; sell/keep flags + CC delta
  planner come later. Do not build yet.

## Going-live seam
`load_book_from_schwab()` is the single live-wiring point. It must return the
same `Book` shape as the fixture. Map Schwab account JSON into
StockPos/OptionPos/Balances there. `CHAI_SOURCE=fixture|schwab` selects source.
Until wired, the whole app runs on the inline fixture / last snapshot.

## Why live beats CSV (context, so you don't reintroduce CSV)
The CSV was a stale, internally-inconsistent snapshot: missing/old option
marks, assignment settlement lag, two disagreeing timestamps in one file. The
API returns the same live data thinkorswim shows — real-time marks, real-time
P&L, settled state, one consistent cash figure. The engine must consume live
marks and settled state from the API, not reconstruct them. Fixture = test and
degraded-mode fallback only. API = truth.
