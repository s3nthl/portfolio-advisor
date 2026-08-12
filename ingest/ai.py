"""Anthropic Messages API bridge for the in-app 'Ask' tab.

Local-first: the user's key lives in .env; their portfolio JSON is sent to Claude
only when they ask a question. Never raises — returns an {error} shape the UI can
show. No dependency beyond httpx.
"""
from __future__ import annotations

import json

import config

_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

# Teaches Claude how to READ this dashboard's numbers so answers stay accurate.
_SYSTEM = """You are the built-in analyst for "s3nthl portfolio dashboard" — a local, \
read-only command board over a Schwab options-wheel account (winding down toward \
buy-and-hold). The user asks questions about the LIVE dashboard state, provided to \
you as JSON below. That JSON is a BUFFER that accumulates data from every tab the \
user has viewed — you can see the whole dashboard, not just one screen.

Context shape (keys present depend on which tabs the user has opened — see `loaded` \
and `active_tab`):
- `dashboard`: the core refresh — summary (net_liq, real_cash, dry_powder, csp_committed, \
cash %, posture/posture_vix with gross + free-cash reads), buckets (stock/csp/covered_call/leap), \
sectors, per-holding breakdown, waterfall, earnings, delta-since-last-refresh.
- `gex`: {SYMBOL: gamma-exposure} — net_gex ($ per 1% move), regime (positive = dealers \
dampen/pin, negative = amplify/trend), flip (zero-gamma spot) + flip_pct, call_wall / put_wall \
(strikes) with their gamma, expiry_view. Computed from the live Schwab chain. SPY is usually present.
- `csp`: {SYMBOL: cash-secured-put candidates} — per DTE bucket, tiers (Conservative/Moderate/\
Aggressive) with strike, delta (~assignment prob), premium, cash_secured, roi_pct (per-cycle, not annualized), breakeven, cushion.
- `covered_calls`: live covered-call analysis per holding (strikes never below cost basis).
- `fundamentals`: {SYMBOL: statement time series + valuation} for tickers the user opened.
- `ideas`: the research-screen rankings.

How to read the numbers (this methodology is exact — honor it):
- CSP exposure is NOTIONAL = strike x 100 x |contracts|, never the option mark.
- dry_powder = real_cash − csp_committed = FREE cash that needs NO margin. It RISES when a CSP is \
closed; negative means margin is backing puts. Option Buying Power is NOT cash. The posture grades \
BOTH gross cash % and free (dry-powder) cash % against the target band.
- Buckets/P&L are separate universes: stock / CSP / LEAP-or-Call / covered-call — never blend them.
- LEAP/Call = long call with > 90 DTE. Spreads are DEFINED-RISK verticals, measured by max_loss.
- Volatility posture: VXN & VIX map to a band (risk-on/caution/fear/elevated/panic) with a target cash %. \
'below' = under-cashed for the regime; 'above' = idle capital.
- GEX: positive net gamma → dealers hedge against the move (pinning, lower realized vol); negative → they \
hedge with the move (trend, higher vol). Spot above the flip = stabilizing; below = unstable.
- Covered calls never written below cost basis; they cap upside and are NOT counted in allocation %.
- Assignment waterfall assumes every short put assigns by expiry; cash drains first, then a margin loan.

Rules for your answers:
- Answer from the JSON provided. If the user asks about a tab they haven't loaded (not in `loaded`), \
say which tab to open (e.g. "open the GEX tab / punch NVDA into CSP Selector") rather than guessing.
- Never invent numbers. Be concise and specific; use $ and % with the ACTUAL values. Bold the key figure.
- You may analyze, compare, flag risks, and explain — but you are NOT a licensed advisor. For any \
"should I buy/sell/trade" question, give the considerations and trade-offs and note the decision is theirs.
- Prefer short paragraphs or tight bullet/numbered lists. Reference the tab a figure comes from when useful."""


def _compact(context: dict, limit: int = 140_000) -> str:
    """JSON-encode the dashboard buffer, trimmed to a sane size for the prompt.

    Drops the heaviest optional blocks in priority order (least-asked first) so
    the core portfolio + options data survives, then hard-truncates as a backstop.
    """
    def dump(c: dict) -> str:
        try:
            return json.dumps(c, separators=(",", ":"), default=str)
        except Exception:
            return "{}"

    s = dump(context)
    if len(s) <= limit:
        return s
    ctx = dict(context)
    for key in ("ideas", "fundamentals"):          # bulky, rarely the question
        ctx.pop(key, None)
        s = dump(ctx)
        if len(s) <= limit:
            return s
    return s[:limit] + "…(truncated)"


def provider() -> str | None:
    """Preferred backend for the Ask tab. Honors CHAI_AI_PROVIDER, else first key."""
    forced = config.CHAI_AI_PROVIDER
    if forced == "openrouter" and config.OPENROUTER_API_KEY:
        return "openrouter"
    if forced == "anthropic" and config.ANTHROPIC_API_KEY:
        return "anthropic"
    if config.ANTHROPIC_API_KEY:
        return "anthropic"
    if config.OPENROUTER_API_KEY:
        return "openrouter"
    return None


def active_model() -> str:
    return config.CHAI_AI_MODEL if provider() == "anthropic" else config.CHAI_AI_MODEL_OR


def _err(r) -> dict:
    if r.status_code in (401, 403):
        return {"error": "auth", "detail": f"provider rejected the key (HTTP {r.status_code})."}
    if r.status_code == 429:
        return {"error": "rate", "detail": "AI provider rate limit — try again shortly."}
    try:
        detail = r.json().get("error", {}).get("message", "") or r.text[:200]
    except Exception:
        detail = r.text[:200]
    # "credit balance too low" comes back as a 400 invalid_request — flag it distinctly
    # so the UI can say "add credits" and so callers reliably fail over to the other provider.
    if r.status_code == 400 and "credit balance" in detail.lower():
        return {"error": "credits",
                "detail": "Anthropic credits exhausted — add credits at console.anthropic.com "
                          "(Plans & Billing), or the app will use your OpenRouter key."}
    return {"error": "api", "detail": f"HTTP {r.status_code}: {detail}"}


def _ask_anthropic(system: str, msgs: list[dict]) -> dict:
    import httpx
    r = httpx.post(_API_URL,
                   headers={"x-api-key": config.ANTHROPIC_API_KEY,
                            "anthropic-version": _ANTHROPIC_VERSION, "content-type": "application/json"},
                   json={"model": config.CHAI_AI_MODEL, "max_tokens": config.CHAI_AI_MAX_TOKENS,
                         "system": system, "messages": msgs}, timeout=90.0)
    if r.status_code == 200:
        j = r.json()
        parts = [b.get("text", "") for b in j.get("content", []) if b.get("type") == "text"]
        txt = "\n".join(parts).strip()
        if not txt:   # empty 200 (e.g. refusal / max_tokens with no text) — a failure, so it fails over
            return {"error": "empty", "detail": f"Claude returned no text (stop_reason={j.get('stop_reason')})."}
        return {"answer": txt, "model": config.CHAI_AI_MODEL, "via": "anthropic"}
    return _err(r)


def _ask_openrouter(system: str, msgs: list[dict]) -> dict:
    import httpx
    r = httpx.post("https://openrouter.ai/api/v1/chat/completions",
                   headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                            "content-type": "application/json",
                            "HTTP-Referer": "https://localhost", "X-Title": "s3nthl portfolio dashboard"},
                   json={"model": config.CHAI_AI_MODEL_OR, "max_tokens": config.CHAI_AI_MAX_TOKENS,
                         "messages": [{"role": "system", "content": system}] + msgs}, timeout=90.0)
    if r.status_code == 200:
        j = r.json()
        if j.get("error"):   # OpenRouter tunnels some errors inside a 200 body
            return {"error": "api", "detail": str(j["error"].get("message") or j["error"])[:200]}
        ch = (j.get("choices") or [{}])[0]
        txt = ((ch.get("message") or {}).get("content", "") or "").strip()
        if not txt:
            return {"error": "empty", "detail": f"OpenRouter returned no text (finish={ch.get('finish_reason')})."}
        return {"answer": txt, "model": config.CHAI_AI_MODEL_OR, "via": "openrouter"}
    return _err(r)


# Once a provider reports exhausted credits, skip it for the rest of the process so
# every subsequent question doesn't eat a guaranteed-failing round-trip first.
_DEAD: set[str] = set()


def _call(which: str, system: str, msgs: list[dict]) -> dict:
    return _ask_anthropic(system, msgs) if which == "anthropic" else _ask_openrouter(system, msgs)


def ask(messages: list[dict], context: dict) -> dict:
    """Send the conversation + dashboard context to Claude, trying both providers.

    Returns {answer, via, model} or {error, detail}. Order: preferred provider first,
    then the other on ANY failure (credits, empty, network) — so a dead Anthropic
    balance transparently rides on the OpenRouter key.
    """
    if not provider():
        return {"error": "no_key",
                "detail": "No AI key configured. Add ANTHROPIC_API_KEY (or OPENROUTER_API_KEY) to your .env."}
    msgs = [{"role": ("assistant" if m.get("role") == "assistant" else "user"),
             "content": str(m.get("content", ""))[:6000]}
            for m in (messages or []) if str(m.get("content", "")).strip()]
    if not msgs:
        return {"error": "empty", "detail": "Ask a question first."}

    system = _SYSTEM + "\n\n=== LIVE DASHBOARD DATA (JSON) ===\n" + _compact(context or {})
    have = {"anthropic": bool(config.ANTHROPIC_API_KEY), "openrouter": bool(config.OPENROUTER_API_KEY)}
    order = [provider()] + [p for p in ("anthropic", "openrouter") if p != provider()]
    order = [p for p in order if have.get(p) and p not in _DEAD]
    if not order:  # both keys previously hit exhausted credits this session
        return {"error": "credits",
                "detail": "Both AI providers are out of credits this session. Add credits to your "
                          "Anthropic or OpenRouter account, then restart the server."}

    last = {"error": "unknown", "detail": "no provider responded."}
    for p in order:
        try:
            res = _call(p, system, msgs)
        except Exception as exc:
            last = {"error": "network", "detail": f"{p}: {exc}"}
            continue
        if "answer" in res:
            return res
        if res.get("error") == "credits":
            _DEAD.add(p)   # don't retry a broke provider this session
        last = res
    return last


# --------------------------------------------------------------------------- #
# Company brief — a crisp "what is this and where's the money" snapshot
# --------------------------------------------------------------------------- #
_BRIEF_SYSTEM = ("You are an equity analyst writing a crisp, plain-English company "
                 "snapshot for a retail investor. Factual, concrete, zero hype/fluff. "
                 "Output ONLY valid minified JSON — no prose, no code fences.")


def _brief_prompt(symbol: str) -> str:
    return (f'For the public company with ticker {symbol}, return JSON exactly:\n'
            '{"name":"company name","does":"one plain sentence — what it actually does",'
            '"different":"one sentence — its edge/moat vs competitors",'
            '"wins":["3 short punchy reasons it is a successful business"],'
            '"revenue":[{"seg":"segment","pct":"~NN%","note":"1-3 words"} ...  main revenue '
            'segments, largest first, approx % of total],'
            '"customers":[{"name":"key customer or customer type","note":"1-4 words e.g. ~20% of rev, or the relationship"} '
            '... the 3-6 biggest actual customers / who buys from it; use real company names '
            '(e.g. for LRCX: TSMC, Samsung, Intel, SK Hynix)],'
            '"demand":"one sentence — what spending/capex cycle actually drives its revenue '
            '(e.g. hyperscaler AI capex, fab equipment spend, consumer upgrade cycle)",'
            '"moat_score":0-100 integer — how wide/durable its competitive moat is '
            '(90+=fortress like a dominant network, 60-80=strong, 30-50=narrow, <30=weak/commodity),'
            '"moat_type":"the primary moat source — one of: Network effects, Switching costs, '
            'Brand & intangibles, Cost advantage, Efficient scale, or None/Narrow"}\n'
            'Keep every field tight. Best-estimate if unsure. JSON only.')


def _parse_brief(text: str) -> dict:
    import json as _json
    import re
    t = (text or "").strip()
    t = re.sub(r'^```(?:json)?', '', t).strip()
    t = re.sub(r'```$', '', t).strip()
    a, b = t.find('{'), t.rfind('}')
    if a >= 0 and b > a:
        try:
            return {"brief": _json.loads(t[a:b + 1])}
        except Exception:
            pass
    return {"error": "parse", "raw": t[:400]}


def company_brief(symbol: str) -> dict:
    """AI-generated plain-English overview for one ticker (uncached). {brief} or {error}."""
    prov = provider()
    if not prov:
        return {"error": "no_key"}
    msgs = [{"role": "user", "content": _brief_prompt(symbol.upper())}]
    res = _ask_anthropic(_BRIEF_SYSTEM, msgs) if prov == "anthropic" else _ask_openrouter(_BRIEF_SYSTEM, msgs)
    if "answer" not in res and config.OPENROUTER_API_KEY and prov == "anthropic":
        res = _ask_openrouter(_BRIEF_SYSTEM, msgs)
    if "answer" not in res:
        return res
    out = _parse_brief(res["answer"])
    out["model"] = res.get("model")
    return out


# --- durable cache (briefs are stable -> keep for weeks) -------------------- #
def _brief_load() -> dict:
    import json as _json
    try:
        return _json.loads(config.COMPANY_BRIEF_CACHE_PATH.read_text())
    except Exception:
        return {}


def _brief_save(cache: dict) -> None:
    import json as _json
    try:
        config.COMPANY_BRIEF_CACHE_PATH.write_text(_json.dumps(cache))
    except Exception:
        pass


def _brief_fresh(entry: dict) -> bool:
    from datetime import date
    if not entry or not entry.get("as_of"):
        return False
    brief = (entry.get("data") or {}).get("brief") or {}
    if not brief or "moat_score" not in brief:   # schema marker: pre-moat briefs are stale
        return False
    try:
        age = (date.today() - date.fromisoformat(entry["as_of"])).days
        return age <= config.CHAI_BRIEF_TTL_DAYS
    except Exception:
        return False


def get_brief(symbol: str, *, force: bool = False, cache: dict | None = None) -> dict:
    """Cached brief (TTL = CHAI_BRIEF_TTL_DAYS). Generates + persists on miss."""
    from datetime import date
    sym = symbol.upper()
    own = cache is None
    if own:
        cache = _brief_load()
    if not force and _brief_fresh(cache.get(sym)):
        return cache[sym]["data"]
    res = company_brief(sym)
    if res.get("brief"):
        cache[sym] = {"as_of": date.today().isoformat(), "data": res}
        if own:
            _brief_save(cache)
    return res


def warm_briefs(symbols, *, force: bool = False, max_workers: int = 5) -> dict:
    """Pre-generate briefs for many tickers, writing them all to the shared cache.

    Returns {generated, skipped, failed, total}. Cheap (short outputs) and rare
    (30-day TTL), so a full warm is a few cents on your AI provider."""
    from concurrent.futures import ThreadPoolExecutor
    syms = sorted({s.upper() for s in symbols if s})
    cache = _brief_load()
    todo = [s for s in syms if force or not _brief_fresh(cache.get(s))]
    generated = failed = 0
    if todo and provider():
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for sym, res in zip(todo, ex.map(lambda s: company_brief(s), todo)):
                if res.get("brief"):
                    cache[sym] = {"as_of": __import__("datetime").date.today().isoformat(), "data": res}
                    generated += 1
                else:
                    failed += 1
        _brief_save(cache)
    return {"generated": generated, "failed": failed,
            "skipped": len(syms) - len(todo), "total": len(syms)}
