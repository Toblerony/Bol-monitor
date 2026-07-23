"""Playwright headless on Render; visible browser on local PC (Settings → Login to Bol)."""
from __future__ import annotations

import os

from sqlalchemy.orm import Session

from app.config import get_settings, is_cloud_host
from app.models import ApplicationSetting


def get_playwright_headless(db: Session | None = None) -> bool:
    if os.environ.get("BOL_LOGIN_MODE", "").strip().lower() in ("1", "true", "yes"):
        return False
    if is_cloud_host():
        return True
    settings = get_settings()
    if settings.PLAYWRIGHT_HEADLESS is not None:
        return settings.PLAYWRIGHT_HEADLESS
    return False


def ensure_visible_browser_setting(db: Session) -> None:
    expected = "true" if is_cloud_host() else "false"
    row = db.query(ApplicationSetting).filter(ApplicationSetting.key == "playwright_headless").first()
    if row is None:
        db.add(ApplicationSetting(key="playwright_headless", value=expected, category="browser"))
    elif is_cloud_host() and row.value != "true":
        row.value = "true"
    elif not is_cloud_host() and row.value != "false":
        row.value = "false"
    db.commit()


def get_playwright_timeout() -> int:
    return get_settings().PLAYWRIGHT_TIMEOUT
