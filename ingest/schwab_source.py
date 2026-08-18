"""The live, read-only Schwab source — the single going-live seam.

`load_book_from_schwab()` returns the SAME `Book` shape as the fixture, so
nothing downstream changes when the source flips. It also dumps the raw account
JSON to `sample_book.json` for inspection and degraded-mode fallback.

Read-only by construction: we only ever call GET endpoints (account numbers,
account positions/balances). No order/trade endpoint is imported or called.

Credentials come from the user's local `.env` (see config.py). The user runs
the browser OAuth flow themselves; creds are never handled in chat.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import config

from .models import Balances, Book, OptionPos, StockPos

# Schwab money-market / cash-sweep funds that count as real cash, not equity.
_MONEY_MARKET_SYMBOLS = {
    "SWVXX", "SNVXX", "SNAXX", "SWGXX", "SGUXX", "SNSXX", "SUTXX", "SCGXX",
}

RAW_DUMP_PATH = config.BASE_DIR / "sample_book.json"


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
def _get_client():
    """Build a read-only schwab-py client.

    Auth flow selection:
      * cached token present  -> reuse it (no browser, auto-refresh).
      * callback host 127.0.0.1 -> automatic login flow (local server).
      * any other callback host (e.g. localhost:8787) -> manual flow, which
        schwab-py allows for non-127.0.0.1 callbacks: you log in, then paste the
        redirected URL back once. This matches the chai-street-web registration.

    Import is local so the package works offline (fixture mode) without pulling
    in schwab-py side effects.
    """
    from urllib.parse import urlparse

    from schwab.auth import (
        client_from_login_flow,
        client_from_manual_flow,
        client_from_token_file,
    )

    if not (config.SCHWAB_API_KEY and config.SCHWAB_APP_SECRET):
        raise RuntimeError(
            "Schwab credentials missing. Set SCHWAB_API_KEY / SCHWAB_APP_SECRET "
            "(or SCHWAB_CLIENT_ID / SCHWAB_CLIENT_SECRET) in your .env, or point "
            "CHAI_SHARED_ENV at a file that has them."
        )

    api_key = config.SCHWAB_API_KEY
    secret = config.SCHWAB_APP_SECRET
    callback = config.SCHWAB_CALLBACK_URL
    token_path = str(config.SCHWAB_TOKEN_PATH)

    # Reuse a cached token when present — no interactive login needed.
    if config.SCHWAB_TOKEN_PATH.exists():
        return client_from_token_file(token_path, api_key, secret)

    host = urlparse(callback).hostname
    if host == "127.0.0.1":
        return client_from_login_flow(api_key, secret, callback, token_path)
    return client_from_manual_flow(api_key, secret, callback, token_path)


def _resolve_account_hash(client) -> str:
    """Pick the account hash to pull (honors SCHWAB_ACCOUNT_HASH if set)."""
    resp = client.get_account_numbers()
    resp.raise_for_status()
    accounts = resp.json()
    if config.SCHWAB_ACCOUNT_HASH:
        return config.SCHWAB_ACCOUNT_HASH
    if not accounts:
        raise RuntimeError("No Schwab accounts returned for these credentials.")
    return accounts[0]["hashValue"]


# --------------------------------------------------------------------------- #
# Parsing helpers (pure)
# --------------------------------------------------------------------------- #
def parse_option_symbol(osi: str) -> tuple[str, str, date, float]:
    """Parse an OSI option symbol -> (underlying, PUT|CALL, expiry, strike).

    Format: 6-char underlying (space-padded) + YYMMDD + C/P + strike*1000 (8).
    e.g. 'AAPL  260918C00235000' -> ('AAPL', 'CALL', 2026-09-18, 235.0)
    """
    underlying = osi[:6].strip()
    yy, mm, dd = int(osi[6:8]), int(osi[8:10]), int(osi[10:12])
    kind = "CALL" if osi[12].upper() == "C" else "PUT"
    strike = int(osi[13:]) / 1000.0
    return underlying, kind, date(2000 + yy, mm, dd), strike


def _position_qty(pos: dict) -> float:
    """Signed quantity: long positive, short negative."""
    return float(pos.get("longQuantity", 0) or 0) - float(pos.get("shortQuantity", 0) or 0)


def _mark_from_market_value(market_value: float, qty: float, multiplier: int) -> float:
    """Derive live per-unit mark from Schwab marketValue (which is a dollar total)."""
    denom = multiplier * abs(qty)
    return round(abs(market_value) / denom, 4) if denom else 0.0


def map_positions(positions: list[dict]) -> tuple[list[StockPos], list[OptionPos], float]:
    """Map raw Schwab positions -> (stocks, options, money_market_total).

    Money-market funds (SWVXX etc.) are pulled OUT of equities and returned as a
    cash total; the caller folds them into real cash per the methodology.
    """
    stocks: list[StockPos] = []
    options: list[OptionPos] = []
    money_market = 0.0

    for pos in positions:
        inst = pos.get("instrument", {})
        asset = inst.get("assetType", "")
        symbol = inst.get("symbol", "")
        qty = _position_qty(pos)
        market_value = float(pos.get("marketValue", 0) or 0)
        avg_price = float(pos.get("averagePrice", 0) or 0)

        if asset == "OPTION":
            underlying, kind, expiry, strike = parse_option_symbol(symbol)
            mark = _mark_from_market_value(market_value, qty, 100)
            # Prefer the API's live open P&L when present.
            api_pl = pos.get("longOpenProfitLoss")
            if api_pl is None:
                api_pl = pos.get("shortOpenProfitLoss")
            options.append(OptionPos(
                symbol=underlying, kind=kind, qty=qty, strike=strike, expiry=expiry,
                trade_price=avg_price, mark=mark,
                _pl_open=(float(api_pl) if api_pl is not None else None),
            ))
        elif symbol in _MONEY_MARKET_SYMBOLS or asset == "MONEY_MARKET":
            money_market += market_value
        elif asset in ("EQUITY", "COLLECTIVE_INVESTMENT", "ETF"):
            mark = _mark_from_market_value(market_value, qty, 1)
            stocks.append(StockPos(symbol=symbol, qty=qty, cost_basis=avg_price, mark=mark))
        # other asset types (fixed income, etc.) are ignored for this book

    return stocks, options, money_market


def map_balances(current: dict, money_market: float) -> Balances:
    """Map Schwab currentBalances -> normalized Balances.

    cash_and_sweep <- cashBalance (Cash & Sweep). Option/Intraday buying power
    are captured for display but are NEVER treated as cash.
    """
    cash = float(current.get("cashBalance", current.get("totalCash", 0)) or 0)
    net_liq = float(current.get("liquidationValue", 0) or 0)
    obp = float(current.get("optionBuyingPower", current.get("buyingPower", 0)) or 0)
    # Schwab reports a drawn margin loan as a negative marginBalance.
    margin_balance = float(current.get("marginBalance", 0) or 0)
    margin_loan = round(-margin_balance, 2) if margin_balance < 0 else 0.0
    return Balances(
        cash_and_sweep=round(cash, 2),
        option_buying_power=round(obp, 2),
        net_liq=round(net_liq, 2),
        margin_loan=margin_loan,
        money_market=round(money_market, 2),
    )


def book_from_account_json(account: dict, as_of: date | None = None) -> Book:
    """Map a full Schwab account payload -> normalized Book (pure)."""
    sec = account.get("securitiesAccount", account)
    positions = sec.get("positions", [])
    current = sec.get("currentBalances", {})
    stocks, options, money_market = map_positions(positions)
    balances = map_balances(current, money_market)
    # Live P/L straight from Schwab (day change + total unrealized).
    balances.day_pl = round(sum(
        float(p.get("currentDayProfitLoss", 0) or 0) for p in positions), 2)
    balances.open_pl = round(sum(
        float(p.get("longOpenProfitLoss", 0) or 0)
        + float(p.get("shortOpenProfitLoss", 0) or 0) for p in positions), 2)
    return Book(stocks=stocks, options=options, balances=balances, as_of=as_of or _today())


def _today() -> date:
    return datetime.now().date()


# --------------------------------------------------------------------------- #
# Live entry point
# --------------------------------------------------------------------------- #
def load_book_from_schwab(dump_path: Path | None = None) -> Book:
    """Pull the live book (read-only) and normalize it. Dumps raw JSON too."""
    client = _get_client()
    account_hash = _resolve_account_hash(client)

    resp = client.get_account(
        account_hash, fields=client.Account.Fields.POSITIONS
    )
    resp.raise_for_status()
    account = resp.json()

    # Dump raw for inspection + degraded-mode fallback.
    out = dump_path or RAW_DUMP_PATH
    out.write_text(json.dumps(account, indent=2, default=str))

    return book_from_account_json(account, as_of=_today())


# Volatility indices — Schwab/ThinkorSwim symbology ($VIX, NOT $VIX.X).
INDEX_SYMBOLS = {"VIX": "$VIX", "VXN": "$VXN"}


def load_index_quotes(mapping: dict[str, str] | None = None) -> dict[str, float]:
    """Fetch live index levels (VIX/VXN) from Schwab — the same feed as ToS.

    Returns {"VIX": 15.99, "VXN": 26.0}. Never raises: a symbol that fails is
    simply omitted so posture can fall back to its stub.
    """
    mapping = mapping or INDEX_SYMBOLS
    client = _get_client()
    out: dict[str, float] = {}
    for name, sym in mapping.items():
        try:
            resp = client.get_quote(sym)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                continue
            q = data[next(iter(data))].get("quote", {})
            val = q.get("lastPrice") or q.get("mark") or q.get("closePrice")
            if val is not None:
                out[name] = float(val)
        except Exception:
            continue
    return out


def _parse_call_chain(d: dict) -> dict:
    """Normalize one Schwab option-chain payload -> {underlying, expirations}."""
    exps = []
    for exp_key, strikes in (d.get("callExpDateMap") or {}).items():
        calls = []
        for slist in strikes.values():
            o = slist[0]
            delta = o.get("delta")
            bid = o.get("bid") or 0.0
            ask = o.get("ask") or 0.0
            mark = o.get("mark") or 0.0
            if delta is None or delta == -999 or bid <= 0:
                continue
            calls.append({
                "strike": o["strikePrice"], "bid": bid, "ask": ask,
                "mark": mark or round((bid + ask) / 2, 2), "delta": delta,
                "oi": o.get("openInterest", 0), "volume": o.get("totalVolume", 0),
                "dte": o.get("daysToExpiration"),
            })
        if calls:
            exps.append({"expiry": exp_key.split(":")[0], "dte": calls[0]["dte"],
                         "calls": sorted(calls, key=lambda c: c["strike"])})
    return {"underlying": d.get("underlyingPrice"),
            "expirations": sorted(exps, key=lambda e: e["dte"])}


def fetch_call_chains(
    symbols, from_days: int = 9, to_days: int = 45, max_workers: int = 8
) -> dict[str, dict]:
    """Fetch live CALL option chains for `symbols` within a DTE window.

    The window spans ~1.5–6 weeks so every DTE bucket (2w/3w/4w/5w) has expiries
    to snap to — one fetch feeds all buckets.

    Fetches concurrently (Schwab tolerates it; httpx.Client is thread-safe), so
    N symbols take ~N/max_workers round-trips instead of N. Dead/illiquid
    contracts (delta sentinel -999 or no bid) are filtered out. Never raises: a
    failed symbol yields empty expirations.
    """
    from concurrent.futures import ThreadPoolExecutor

    client = _get_client()
    today = _today()
    frm, to = today + timedelta(days=from_days), today + timedelta(days=to_days)

    def one(sym: str) -> tuple[str, dict]:
        try:
            resp = client.get_option_chain(
                sym, contract_type=client.Options.ContractType.CALL,
                from_date=frm, to_date=to,
            )
            resp.raise_for_status()
            return sym, _parse_call_chain(resp.json())
        except Exception:
            return sym, {"underlying": None, "expirations": []}

    symbols = list(symbols)
    workers = max(1, min(max_workers, len(symbols)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(ex.map(one, symbols))


def _parse_put_chain(d: dict) -> dict:
    """Normalize a Schwab PUT chain payload -> {underlying, expirations[.puts]}."""
    exps = []
    for exp_key, strikes in (d.get("putExpDateMap") or {}).items():
        puts = []
        for slist in strikes.values():
            o = slist[0]
            delta = o.get("delta")
            bid = o.get("bid") or 0.0
            ask = o.get("ask") or 0.0
            mark = o.get("mark") or 0.0
            if delta is None or delta == -999 or bid <= 0:
                continue
            puts.append({
                "strike": o["strikePrice"], "bid": bid, "ask": ask,
                "mark": mark or round((bid + ask) / 2, 2), "delta": delta,  # puts: delta < 0
                "oi": o.get("openInterest", 0), "volume": o.get("totalVolume", 0),
                "dte": o.get("daysToExpiration"),
            })
        if puts:
            exps.append({"expiry": exp_key.split(":")[0], "dte": puts[0]["dte"],
                         "puts": sorted(puts, key=lambda c: c["strike"])})
    return {"underlying": d.get("underlyingPrice"),
            "expirations": sorted(exps, key=lambda e: e["dte"])}


def fetch_put_chain(symbol: str, from_days: int = 9, to_days: int = 45) -> dict:
    """Live PUT chain for ONE symbol within the DTE window (for the CSP selector).

    Returns {"underlying": spot, "expirations":[{expiry,dte,puts[...]}]}. Never
    raises: empty on failure.
    """
    try:
        client = _get_client()
        today = _today()
        frm, to = today + timedelta(days=from_days), today + timedelta(days=to_days)
        resp = client.get_option_chain(
            symbol, contract_type=client.Options.ContractType.PUT,
            from_date=frm, to_date=to,
        )
        resp.raise_for_status()
        return _parse_put_chain(resp.json())
    except Exception:
        return {"underlying": None, "expirations": []}


def _parse_full_chain(d: dict) -> dict:
    """Normalize a Schwab ALL-contracts chain -> {underlying, expirations[.calls/.puts]}.

    Unlike the CSP/CC parsers this keeps GAMMA + IV and does NOT drop zero-bid
    contracts — deep-OTM strikes still carry open interest and gamma, which GEX
    needs. Contracts are kept when gamma is present and open interest > 0.
    """
    def rows(exp_map: dict) -> dict[str, list]:
        out: dict[str, list] = {}
        for exp_key, strikes in (exp_map or {}).items():
            legs = []
            for slist in strikes.values():
                o = slist[0]
                gamma = o.get("gamma")
                oi = o.get("openInterest", 0) or 0
                if gamma is None or gamma == -999 or oi <= 0:
                    continue
                iv = o.get("volatility")  # Schwab reports IV in percent
                legs.append({
                    "strike": o["strikePrice"], "oi": oi, "gamma": gamma,
                    "iv": (iv / 100.0) if iv and iv != -999 else None,
                    "delta": o.get("delta"), "volume": o.get("totalVolume", 0),
                    "dte": o.get("daysToExpiration"),
                })
            if legs:
                out[exp_key.split(":")[0]] = sorted(legs, key=lambda c: c["strike"])
        return out

    calls = rows(d.get("callExpDateMap"))
    puts = rows(d.get("putExpDateMap"))
    exps = []
    for exp in sorted(set(calls) | set(puts)):
        c, p = calls.get(exp, []), puts.get(exp, [])
        dte = (c or p)[0]["dte"] if (c or p) else None
        exps.append({"expiry": exp, "dte": dte, "calls": c, "puts": p})
    return {"underlying": d.get("underlyingPrice"),
            "expirations": sorted(exps, key=lambda e: (e["dte"] is None, e["dte"]))}


def fetch_full_chain(symbol: str, from_days: int = 0, to_days: int = 75,
                     strike_count: int = 300) -> dict:
    """Live full option chain (calls + puts, with gamma/IV) for ONE symbol.

    Powers the GEX board. `strike_count` centers on ATM by COUNT; near-money
    strikes are densely spaced (1-wide on SPY) so a generous count is needed to
    reach ~±20% of spot on the PUT side — the downstream ±band filter then trims
    both wings to a symmetric price window. Never raises: empty on any failure.
    """
    try:
        client = _get_client()
        today = _today()
        frm, to = today + timedelta(days=from_days), today + timedelta(days=to_days)
        resp = client.get_option_chain(
            symbol, contract_type=client.Options.ContractType.ALL,
            strike_count=strike_count, from_date=frm, to_date=to,
        )
        resp.raise_for_status()
        return _parse_full_chain(resp.json())
    except Exception:
        return {"underlying": None, "expirations": []}


def fetch_ytd_external_flows(start_date: str, end_date: str | None = None) -> float:
    """Net external cash flow (deposits − withdrawals) since `start_date` (YYYY-MM-DD).

    Sums netAmount over cash-transfer transaction types so YTD P/L can back them
    out. Cached daily to a JSON file to avoid re-pulling on every refresh. Never
    raises: returns 0.0 on any failure (YTD then ignores flows).
    """
    from datetime import datetime as _dt

    cache_path = config.BASE_DIR / "ytd_flows_cache.json"
    today = _today().isoformat()
    key = f"{start_date}:{end_date or today}"
    try:
        cache = json.loads(cache_path.read_text())
        if cache.get("as_of") == today and cache.get("key") == key:
            return float(cache.get("net_flow", 0.0))
    except Exception:
        pass

    try:
        client = _get_client()
        account_hash = _resolve_account_hash(client)
        start = _dt.fromisoformat(start_date)
        end = _dt.fromisoformat(end_date) if end_date else _dt.now()
        T = client.Transactions.TransactionType
        flow_types = [T.ACH_RECEIPT, T.ACH_DISBURSEMENT, T.WIRE_IN, T.WIRE_OUT,
                      T.CASH_RECEIPT, T.CASH_DISBURSEMENT, T.ELECTRONIC_FUND, T.JOURNAL]
        total = 0.0
        for tt in flow_types:
            try:
                r = client.get_transactions(account_hash, start_date=start,
                                            end_date=end, transaction_types=tt)
                r.raise_for_status()
                for tx in r.json():
                    total += float(tx.get("netAmount", 0) or 0)
            except Exception:
                continue
        total = round(total, 2)
        cache_path.write_text(json.dumps({"as_of": today, "key": key, "net_flow": total}))
        return total
    except Exception:
        return 0.0


def monthly_closes(symbol: str, years: int = 10) -> list[dict]:
    """Monthly closing prices for `symbol` over ~`years` years (Schwab, free).

    Returns [{"date": "YYYY-MM", "close": float}, ...] oldest->newest. Never
    raises: returns [] on failure.
    """
    from datetime import date as _d
    try:
        client = _get_client()
        PH = client.PriceHistory
        period = PH.Period.TEN_YEARS if years <= 10 else PH.Period.FIFTEEN_YEARS
        resp = client.get_price_history(
            symbol, period_type=PH.PeriodType.YEAR, period=period,
            frequency_type=PH.FrequencyType.MONTHLY, frequency=PH.Frequency.EVERY_MINUTE,
        )
        resp.raise_for_status()
        out = []
        for c in resp.json().get("candles", []):
            d = _d.fromtimestamp(c["datetime"] / 1000)
            out.append({"date": f"{d.year}-{d.month:02d}", "close": round(c["close"], 2)})
        return out
    except Exception:
        return []


def daily_closes(symbol: str, years: int = 5) -> list[dict]:
    """Daily closing prices for `symbol` over ~`years` years (Schwab, free).

    Returns [{"date": "YYYY-MM-DD", "close": float}, ...] oldest->newest. Used to
    measure the price reaction around each earnings date. Never raises: [] on error.
    """
    from datetime import date as _d
    try:
        client = _get_client()
        PH = client.PriceHistory
        period = (PH.Period.THREE_YEARS if years <= 3 else
                  PH.Period.FIVE_YEARS if years <= 5 else PH.Period.TEN_YEARS)
        resp = client.get_price_history(
            symbol, period_type=PH.PeriodType.YEAR, period=period,
            frequency_type=PH.FrequencyType.DAILY, frequency=PH.Frequency.DAILY,
        )
        resp.raise_for_status()
        out = []
        for c in resp.json().get("candles", []):
            d = _d.fromtimestamp(c["datetime"] / 1000)
            out.append({"date": d.isoformat(), "close": round(c["close"], 2)})
        return out
    except Exception:
        return []


def batch_quotes(symbols: list[str]) -> dict[str, float]:
    """Current price for many symbols in one call. {symbol: last_price}. [] safe."""
    if not symbols:
        return {}
    try:
        client = _get_client()
        resp = client.get_quotes(list(symbols))
        resp.raise_for_status()
        out = {}
        for sym, row in (resp.json() or {}).items():
            q = row.get("quote") or {}
            px = q.get("lastPrice") or q.get("mark") or q.get("closePrice")
            if px:
                out[sym] = round(float(px), 2)
        return out
    except Exception:
        return {}


def instrument_fundamentals(symbol: str) -> dict:
    """Real-time fundamental snapshot for `symbol` from Schwab (no external limit).

    Returns a subset of Schwab's instrument `fundamental` block (P/E, market cap,
    52-wk high/low, dividend yield, EPS, description) plus the instrument's asset
    type — enough to render a company card even when deep statements are missing.
    Never raises: returns {} on failure.
    """
    try:
        client = _get_client()
        resp = client.get_instruments(
            [symbol], projection=client.Instrument.Projection.FUNDAMENTAL,
        )
        resp.raise_for_status()
        rows = resp.json().get("instruments") or []
        if not rows:
            return {}
        inst = rows[0]
        f = inst.get("fundamental") or {}
        return {
            "name": inst.get("description"), "asset_type": inst.get("assetType"),
            "exchange": inst.get("exchange"),
            "pe": f.get("peRatio"), "market_cap": f.get("marketCap"),
            "high_52": f.get("high52"), "low_52": f.get("low52"),
            "div_yield": f.get("divYield"), "div_amount": f.get("divAmount"),
            "eps": f.get("eps"), "pb": f.get("pbRatio"), "pcf": f.get("pcfRatio"),
            "beta": f.get("beta"),
        }
    except Exception:
        return {}


def rich_quotes(symbols: list[str]) -> dict:
    """Live quote snapshot per symbol for the watchlist: last price, today's %
    move, and the 52-week high/low (for the dip-from-high metric). One batch
    call. Read-only. Handles class-B tickers (BRK.B -> Schwab 'BRK/B')."""
    if not symbols:
        return {}
    client = _get_client()
    req = {s: s.replace(".", "/") for s in symbols}      # BRK.B -> BRK/B
    out: dict = {}
    try:
        r = client.get_quotes(list(req.values()))
        j = r.json() if r.status_code == 200 else {}
    except Exception:
        j = {}
    for sym, rq in req.items():
        d = j.get(rq) or j.get(sym) or {}
        q = d.get("quote") or {}
        if not q:
            continue
        out[sym] = {
            "price": q.get("lastPrice"), "chg": q.get("netChange"),
            "chg_pct": q.get("netPercentChange"),
            "high52": q.get("52WeekHigh"), "low52": q.get("52WeekLow"),
            "open": q.get("openPrice"), "volume": q.get("totalVolume"),
        }
    return out


def fetch_option_transactions(years: int = 3) -> list[dict]:
    """Read-only pull of the account's OPTION trade history (for realized P&L).

    Schwab's transaction endpoint returns at most ~1 year per call, so for a
    LIFETIME view we walk back one 1-year window at a time (up to `years`) and
    dedupe by activityId. Returns one record per option transaction (Schwab
    cleanly separates option trades from stock — each has one OPTION leg + a cash
    leg), with `net` = the signed realized cash flow AFTER fees: sell-to-open
    premium is positive, a buy-to-close is negative, assignments/expirations
    (RECEIVE_AND_DELIVER) are 0 (premium already booked at open). `strike` is
    parsed from the OSI symbol for capital/ROC. GET only — no order endpoint.
    """
    client = _get_client()
    h = _resolve_account_hash(client)
    now = datetime.now(timezone.utc)
    seen: set = set()
    out: list[dict] = []
    got_any = False
    for yr in range(max(1, years)):
        end = now - timedelta(days=365 * yr)
        start = end - timedelta(days=365)
        try:
            resp = client.get_transactions(h, start_date=start, end_date=end)
        except Exception:
            continue
        if resp.status_code != 200:
            if not got_any and yr == 0:
                raise RuntimeError(f"Schwab transactions HTTP {resp.status_code}")
            continue
        window_opts = 0
        for t in resp.json():
            aid = t.get("activityId")
            if aid is not None and aid in seen:
                continue
            legs = t.get("transferItems") or []
            opt_legs = [ti for ti in legs
                        if (ti.get("instrument") or {}).get("assetType") == "OPTION"]
            if not opt_legs:
                continue
            if aid is not None:
                seen.add(aid)
            window_opts += 1
            leg = opt_legs[0]                    # transactions carry a single underlying
            ins = leg.get("instrument") or {}
            try:
                contracts = float(leg.get("amount") or 0)
            except Exception:
                contracts = 0.0
            strike = None
            try:
                _, _, _, strike = parse_option_symbol(ins.get("symbol") or "")
            except Exception:
                strike = None
            out.append({
                "date": (t.get("tradeDate") or "")[:10],
                "type": t.get("type"),               # TRADE | RECEIVE_AND_DELIVER
                "position_id": t.get("position_id") or t.get("positionId"),
                "underlying": ins.get("underlyingSymbol") or ins.get("symbol"),
                "kind": ins.get("putCall"),          # PUT | CALL
                "symbol": ins.get("symbol"),
                "strike": strike,
                "contracts": contracts,               # signed (+long / -short)
                "effect": leg.get("positionEffect"),  # OPENING | CLOSING
                "net": round(float(t.get("netAmount") or 0), 2),
            })
        got_any = got_any or window_opts > 0
        if window_opts == 0 and yr > 0:          # walked past the account's history
            break
    return out


def fetch_stock_transactions(years: int = 3) -> list[dict]:
    """Read-only pull of EQUITY / ETF trade history (for realized stock P&L).

    Same 1-year-window stitching + activityId dedupe as the option pull. Returns
    one record per equity leg: `shares` signed (+acquire / −dispose), `price`, and
    `net` = the leg's signed cash (a buy is negative, a sell's proceeds positive).
    Includes assignment deliveries (a CSP-assigned share's basis is its strike) but
    EXCLUDES the SWVXX-style money-market cash sweep (that's cash, not a trade).
    GET only — no order endpoint.
    """
    client = _get_client()
    h = _resolve_account_hash(client)
    now = datetime.now(timezone.utc)
    seen: set = set()
    out: list[dict] = []
    got_any = False
    STOCK = {"EQUITY", "COLLECTIVE_INVESTMENT", "ETF", "MUTUAL_FUND"}
    SWEEP = {"SWVXX", "SNVXX", "SNSXX", "SGOV"}   # money-market sweeps, not P&L trades
    for yr in range(max(1, years)):
        end = now - timedelta(days=365 * yr)
        start = end - timedelta(days=365)
        try:
            resp = client.get_transactions(h, start_date=start, end_date=end)
        except Exception:
            continue
        if resp.status_code != 200:
            if not got_any and yr == 0:
                raise RuntimeError(f"Schwab transactions HTTP {resp.status_code}")
            continue
        window = 0
        for t in resp.json():
            aid = t.get("activityId")
            if aid is not None and aid in seen:
                continue
            legs = t.get("transferItems") or []
            eq = [ti for ti in legs
                  if (ti.get("instrument") or {}).get("assetType") in STOCK
                  and ((ti.get("instrument") or {}).get("symbol") or "") not in SWEEP]
            if not eq:
                continue
            if aid is not None:
                seen.add(aid)
            window += 1
            for leg in eq:
                ins = leg.get("instrument") or {}
                try:
                    shares = float(leg.get("amount") or 0)
                except Exception:
                    shares = 0.0
                if abs(shares) < 1e-9:
                    continue
                try:
                    price = float(leg.get("price") or 0)
                except Exception:
                    price = 0.0
                cost = leg.get("cost")               # per-leg signed cash when present
                net = float(cost) if cost is not None else float(t.get("netAmount") or 0)
                out.append({
                    "date": (t.get("tradeDate") or "")[:10],
                    "type": t.get("type"),           # TRADE | RECEIVE_AND_DELIVER
                    "symbol": ins.get("symbol"),
                    "shares": shares,                # +acquire / -dispose
                    "price": price,
                    "net": round(net, 2),            # signed cash (buy −, sell +)
                })
        got_any = got_any or window > 0
        if window == 0 and yr > 0:
            break
    return out


def load_book_from_snapshot(path: Path | None = None) -> Book:
    """Degraded-mode fallback: rebuild the Book from the last raw dump."""
    src = path or RAW_DUMP_PATH
    if not src.exists():
        raise FileNotFoundError(f"No snapshot to fall back to at {src}")
    account = json.loads(src.read_text())
    return book_from_account_json(account, as_of=_today())
