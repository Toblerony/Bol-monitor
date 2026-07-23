"""Playwright Chromium — local folder or Render Docker image."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from app.config import is_cloud_host

logger = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
BROWSERS_DIR = BACKEND_ROOT / "playwright-browsers"


def configure_playwright_browsers_path() -> Path:
    BROWSERS_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR.resolve())
    return BROWSERS_DIR


def chromium_executable() -> Path | None:
    configure_playwright_browsers_path()
    patterns = (
        "chromium-*/chrome-win64/chrome.exe",
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        "chromium-*/chrome",
    )
    for pattern in patterns:
        for candidate in BROWSERS_DIR.glob(pattern):
            if candidate.is_file():
                return candidate
    ms_path = Path("/ms-playwright")
    if ms_path.exists():
        for pattern in ("chromium-*/chrome-linux/chrome", "chromium-*/chrome"):
            for candidate in ms_path.glob(pattern):
                if candidate.is_file():
                    return candidate
    return None


def is_chromium_installed() -> bool:
    if chromium_executable() is not None:
        return True
    if is_cloud_host() or Path("/ms-playwright").exists():
        return True
    return False


def ensure_chromium_installed(*, quiet: bool = False) -> Path:
    """Download Chromium into backend/playwright-browsers if missing (~180 MB one-time)."""
    configure_playwright_browsers_path()
    exe = chromium_executable()
    if exe is not None:
        return exe

    if not quiet:
        print(f"Playwright Chromium not found — installing into {BROWSERS_DIR}")
        print("One-time download (~180 MB). Please wait…")

    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR.resolve())
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Playwright Chromium install failed. Check internet and re-run setup.bat"
        )

    exe = chromium_executable()
    if exe is None:
        raise RuntimeError(
            f"Chromium install finished but chrome.exe not found under {BROWSERS_DIR}"
        )

    if not quiet:
        print(f"Playwright Chromium ready: {exe}")
    return exe
