"""Ensure Playwright Chromium is installed into backend/playwright-browsers/."""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.playwright_browsers import BROWSERS_DIR, ensure_chromium_installed, is_chromium_installed


def main() -> int:
    try:
        if is_chromium_installed():
            print(f"Playwright Chromium already installed: {BROWSERS_DIR}")
            return 0
        ensure_chromium_installed()
        return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
