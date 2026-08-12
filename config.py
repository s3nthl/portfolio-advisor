"""Central configuration for ChaiStreet Command.

Credential resolution (first match wins):
  1. process environment
  2. local ./.env            (machine-specific, gitignored)
  3. a shared .env pointed to by CHAI_SHARED_ENV (e.g. an existing
     chai-street-web/.env) — lets this app reuse creds without copying secrets.

Schwab keys are accepted under either naming convention:
  SCHWAB_API_KEY     | SCHWAB_CLIENT_ID
  SCHWAB_APP_SECRET  | SCHWAB_CLIENT_SECRET
  SCHWAB_CALLBACK_URL| SCHWAB_REDIRECT_URI

No hard-coded absolute paths live in this file, so it stays portable into the
ChaiStreet monorepo. Point CHAI_SHARED_ENV at the shared file from your local
.env (which is gitignored, so an absolute path there is fine).
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Resolve paths relative to this file, never the CWD, so the module is portable.
BASE_DIR = Path(__file__).resolve().parent

# 1) local .env takes precedence (load_dotenv never overrides existing vars)
load_dotenv(BASE_DIR / ".env")
# 2) shared .env fills in anything still unset (e.g. reuse chai-street-web creds)
_shared_env = os.getenv("CHAI_SHARED_ENV")
if _shared_env:
    load_dotenv(Path(_shared_env).expanduser(), override=False)


def _first(*names: str, default: str = "") -> str:
    """Return the first non-empty environment value among `names`."""
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default


def _path(env_value: str) -> Path:
    """Resolve a possibly-relative path against BASE_DIR."""
    p = Path(env_value).expanduser()
    return p if p.is_absolute() else BASE_DIR / p


# --- Data source ---
# "fixture" (offline test/fallback) or "schwab" (live read-only runtime source).
CHAI_SOURCE = os.getenv("CHAI_SOURCE", "fixture").strip().lower()

# --- Schwab (read-only) — accept both naming conventions ---
SCHWAB_API_KEY = _first("SCHWAB_API_KEY", "SCHWAB_CLIENT_ID")
SCHWAB_APP_SECRET = _first("SCHWAB_APP_SECRET", "SCHWAB_CLIENT_SECRET")
SCHWAB_CALLBACK_URL = _first(
    "SCHWAB_CALLBACK_URL", "SCHWAB_REDIRECT_URI", default="https://127.0.0.1:8182"
)
SCHWAB_TOKEN_PATH = _path(os.getenv("SCHWAB_TOKEN_PATH", "schwab_token.json"))
SCHWAB_ACCOUNT_HASH = os.getenv("SCHWAB_ACCOUNT_HASH", "")

# --- Finnhub (live sector + beta enrichment; optional) ---
FINNHUB_API_KEY = _first("FINNHUB_API_KEY")
FINNHUB_CACHE_PATH = _path(os.getenv("FINNHUB_CACHE_PATH", "finnhub_cache.json"))
FINNHUB_CACHE_DAYS = int(os.getenv("FINNHUB_CACHE_DAYS", "7"))

# --- Financial Modeling Prep (next-earnings-date; optional) ---
FMP_API_KEY = _first("FMP_API_KEY")
FMP_CACHE_PATH = _path(os.getenv("FMP_CACHE_PATH", "fmp_earnings_cache.json"))
FMP_FUND_CACHE_PATH = _path(os.getenv("FMP_FUND_CACHE_PATH", "fmp_fundamentals_cache.json"))
FMP_CACHE_DAYS = int(os.getenv("FMP_CACHE_DAYS", "1"))
# Statement history depth. FMP FREE tier caps this at 5 periods; raise on a paid plan.
FMP_STMT_LIMIT = int(os.getenv("FMP_STMT_LIMIT", "5"))

# --- Polygon / Massive (paid: options + deep financials) ---
POLYGON_API_KEY = _first("POLYGON_API_KEY")
POLYGON_FUND_CACHE_PATH = _path(os.getenv("POLYGON_FUND_CACHE_PATH", "polygon_fund_cache.json"))
POLYGON_FUND_LIMIT = int(os.getenv("POLYGON_FUND_LIMIT", "16"))  # years of statements
SCREENER_CACHE_PATH = _path(os.getenv("SCREENER_CACHE_PATH", "screener_cache.json"))
# Flag next earnings as "imminent" when within this many days.
EARNINGS_ALERT_DAYS = int(os.getenv("EARNINGS_ALERT_DAYS", "14"))

# --- AI assistant (in-app "Ask" tab; optional) ---
# Two ways to power it (first present wins):
#   1) ANTHROPIC_API_KEY  -> Claude direct (model = CHAI_AI_MODEL, e.g. claude-opus-4-8)
#   2) OPENROUTER_API_KEY -> Claude via OpenRouter (model = CHAI_AI_MODEL_OR, an
#      OpenRouter slug like anthropic/claude-3.5-sonnet). Reuses a key you may
#      already have. Local-first: keys live in .env (gitignored), never in chat.
ANTHROPIC_API_KEY = _first("ANTHROPIC_API_KEY", "CLAUDE_API_KEY")
OPENROUTER_API_KEY = _first("OPENROUTER_API_KEY")
CHAI_AI_MODEL = os.getenv("CHAI_AI_MODEL", "claude-sonnet-5")           # Anthropic-direct id
CHAI_AI_MODEL_OR = os.getenv("CHAI_AI_MODEL_OR", "anthropic/claude-sonnet-5")  # OpenRouter slug (opus: anthropic/claude-opus-4.8)
# Force a provider: "anthropic" | "openrouter" | "" (auto: Anthropic first, then
# fall back to OpenRouter if Anthropic errors on auth/billing).
CHAI_AI_PROVIDER = os.getenv("CHAI_AI_PROVIDER", "").strip().lower()
CHAI_AI_MAX_TOKENS = int(os.getenv("CHAI_AI_MAX_TOKENS", "4000"))   # room for a thorough answer
# Company briefs are stable (business models don't change daily) -> cache for weeks.
COMPANY_BRIEF_CACHE_PATH = _path(os.getenv("COMPANY_BRIEF_CACHE_PATH", "company_brief_cache.json"))
CHAI_BRIEF_TTL_DAYS = int(os.getenv("CHAI_BRIEF_TTL_DAYS", "30"))

# --- Risk assumptions ---
# Schwab margin interest rate (annual, decimal). Tiered ~11-13%; override in .env.
CHAI_MARGIN_RATE = float(os.getenv("CHAI_MARGIN_RATE", "0.12"))

# --- YTD P/L baseline ---
# Your account's Net Liq at the start of the year (0 = unset -> YTD hidden).
# YTD P/L = current net liq - this - net external cash flows (auto-computed).
CHAI_YTD_START_NETLIQ = float(os.getenv("CHAI_YTD_START_NETLIQ", "0"))
CHAI_YTD_START_DATE = os.getenv("CHAI_YTD_START_DATE", "2026-01-01")

# --- Store ---
CHAI_DB_PATH = _path(os.getenv("CHAI_DB_PATH", "chai_command.db"))

# --- Server ---
CHAI_HOST = os.getenv("CHAI_HOST", "127.0.0.1")
CHAI_PORT = int(os.getenv("CHAI_PORT", "8000"))
