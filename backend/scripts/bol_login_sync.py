"""One-time Bol.com login on your PC — saves session to database for Render headless bot."""
from __future__ import annotations

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
from app.services.bol_session import get_bol_session_status, open_bol_login_browser
from app.startup_db import run_blocking_startup

configure_playwright_browsers_path()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> int:
    settings = get_settings()
    print("=" * 60)
    print("Bol Login — visible Chromium window will open")
    print("Log in to bol.com. Session saves to Neon database.")
    print("Use the SAME DATABASE_URL on PC and Render.")
    print("After this, Render runs monitoring headless.")
    print("=" * 60)

    run_blocking_startup(settings)

    try:
        ensure_chromium_installed()
        ok = await open_bol_login_browser(settings)
        status = get_bol_session_status()
        if ok and status.get("has_session"):
            print("\nSUCCESS — Bol session saved to Neon database.")
            print("Dashboard: Start monitoring (header Start button).")
            return 0
        if status.get("has_session") and not ok:
            print("\nWARNING — old session may still be in database.")
            print("Settings → Clear Bol session, then run login-bol.bat again.")
            return 1
        print("\nFAILED — login not completed. Header must show Welkom <name>.")
        return 1
    except Exception as exc:
        logger.exception("Bol login failed: %s", exc)
        print(f"\nERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
