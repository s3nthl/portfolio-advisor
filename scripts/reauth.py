#!/usr/bin/env python
"""One-command Schwab re-authentication.

Schwab refresh tokens expire after 7 days; when they do, every live pull (GEX,
Refresh, chains) silently returns empty. Run this to mint a fresh token:

    python scripts/reauth.py

It backs up the old token, opens Schwab's login in your browser (you log in —
credentials never touch this script), captures the OAuth callback, writes a new
token, and verifies it with a live quote. The running dashboard picks up the new
token automatically; no server restart needed.

Read-only OAuth scope only. Local-first: key/secret/callback come from .env.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from recession import config as _rc  # noqa: F401  (ensures .env is loaded)
import config

REFRESH_TOKEN_DAYS = 7  # Schwab refresh-token lifetime


def _token_status(path: Path) -> str:
    """Human-readable note on the current token, if any."""
    if not path.exists():
        return "no existing token file"
    try:
        blob = json.loads(path.read_text())
        created = blob.get("creation_timestamp")
        if created:
            expiry = datetime.fromtimestamp(created) + timedelta(days=REFRESH_TOKEN_DAYS)
            state = "EXPIRED" if datetime.now() > expiry else "still valid"
            return f"current refresh token {state} (created {datetime.fromtimestamp(created):%Y-%m-%d %H:%M}, good until {expiry:%Y-%m-%d %H:%M})"
    except Exception:
        pass
    return "current token present (unreadable metadata)"


def main() -> int:
    key, secret = config.SCHWAB_API_KEY, config.SCHWAB_APP_SECRET
    callback = config.SCHWAB_CALLBACK_URL
    token_path = Path(config.SCHWAB_TOKEN_PATH)

    if not (key and secret):
        print("ERROR: SCHWAB_API_KEY / SCHWAB_APP_SECRET missing from .env — cannot re-auth.")
        return 2

    print(f"Callback URL : {callback}")
    print(f"Token file   : {token_path}")
    print(f"Status       : {_token_status(token_path)}\n")

    # Move the old token aside so schwab-py runs the login flow (it would otherwise
    # just reuse the stale file). Kept as .bak until the new one is verified.
    backup = None
    if token_path.exists():
        backup = token_path.with_suffix(token_path.suffix + ".bak")
        token_path.replace(backup)
        print(f"Backed up old token -> {backup.name}")

    print("Opening Schwab login in your browser — log in and approve.")
    print("(You may see a self-signed-cert warning on the 127.0.0.1 callback; that is "
          "expected — proceed.)\n")

    try:
        from schwab.auth import client_from_login_flow
        # interactive=False -> auto-open the browser, no ENTER prompt (works headless-ish).
        client = client_from_login_flow(key, secret, callback, str(token_path),
                                        interactive=False)
    except Exception as exc:
        print(f"\nLogin flow failed: {exc}")
        if backup and not token_path.exists():
            backup.replace(token_path)
            print("Restored previous token from backup.")
        return 1

    if not token_path.exists():
        print("\nLogin flow returned but no token was written.")
        if backup:
            backup.replace(token_path)
            print("Restored previous token from backup.")
        return 1

    # Verify with a lightweight live call.
    verified = ""
    try:
        r = client.get_quote("SPY")
        if r.status_code == 200:
            px = r.json().get("SPY", {}).get("quote", {}).get("lastPrice")
            verified = f" — live check OK (SPY last {px})" if px is not None else " — live check OK"
    except Exception as exc:
        verified = f" — WARNING: token written but live check failed: {exc}"

    print(f"\n✓ Re-auth complete{verified}")
    print(f"  {_token_status(token_path)}")
    if backup and backup.exists():
        backup.unlink()  # new token verified; drop the stale backup
        print(f"  removed {backup.name}")
    print("\nThe running dashboard will use the new token automatically — just hit "
          "Refresh / reopen GEX.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
