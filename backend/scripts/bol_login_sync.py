"""One-time Bol.com login on your PC — saves session to database for Render headless bot."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

os.environ["BOL_LOGIN_MODE"] = "1"

from app.config import get_settings
from app.playwright_browsers import configure_playwright_browsers_path, ensure_chromium_installed
from app.services.bol_session import (
    get_bol_session_status,
    open_bol_login_browser,
    restore_session_file_from_db,
    session_file,
    _verify_stored_session_async,
)
from app.startup_db import run_blocking_startup

configure_playwright_browsers_path()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _session_still_valid(settings) -> bool:
    """Load Neon session into local file and verify Welkom is still shown."""
    restore_session_file_from_db(settings)
    sf = session_file(settings)
    if not sf.is_file() or sf.stat().st_size < 20:
        return False
    try:
        return await _verify_stored_session_async(settings, sf)
    except Exception as exc:
        logger.warning("Existing session verify failed: %s", exc)
        return False


async def main() -> int:
    parser = argparse.ArgumentParser(description="Bol.com login → save session to Neon")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore existing valid session and open a fresh login browser",
    )
    args = parser.parse_args()

    settings = get_settings()
    print("=" * 60)
    print("Bol Login — session persists in Neon (not lost every run)")
    print("Use the SAME DATABASE_URL on PC and Render.")
    print("If Proxies are ON, verify/login uses those proxies.")
    print("=" * 60)
    print()
    print("FLOW:")
    print("  • Session already valid → this script exits OK (no re-login)")
    print("  • Session missing/expired → browser opens for login")
    print("  • Force new login → python scripts\\bol_login_sync.py --force")
    print("=" * 60)

    run_blocking_startup(settings)
    status = get_bol_session_status()

    try:
        ensure_chromium_installed()

        if status.get("has_database") and not args.force:
            print()
            print("Session found in database — checking if it is still logged in…")
            if await _session_still_valid(settings):
                print()
                print("SUCCESS — existing session is still valid (Welkom).")
                print("No re-login needed. Dashboard → Start monitoring.")
                print("Tip: only use --force if you intentionally want a new login.")
                return 0
            print("Existing session is expired / logged out — opening login browser…")
        elif args.force:
            print()
            print("--force: opening a fresh login browser (old cookies not loaded).")

        ok = await open_bol_login_browser(settings)
        status = get_bol_session_status()
        if ok and status.get("has_session"):
            print("\nSUCCESS — Bol session saved to Neon database.")
            print("Dashboard: Start monitoring (header Start button).")
            print("Next time use Settings → Login to Bol; it will reuse this session if still valid.")
            return 0
        if status.get("has_session") and not ok:
            print("\nWARNING — login did not finish, but an older session is still in the database.")
            print("If that old session still works, use Start monitoring.")
            print("If not: Settings → Clear Bol session, then Login to Bol again.")
            return 1
        print("\nFAILED — login not completed. Header must show Welkom <name>.")
        return 1
    except Exception as exc:
        logger.exception("Bol login failed: %s", exc)
        print(f"\nERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
