"""Blocking DB startup."""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from app.config import Settings
from app.database import Base, SessionLocal, engine
from app.seeds.seed_data import seed_database
from app.models import MonitoringSetting
from app.services.browser_settings import ensure_visible_browser_setting

logger = logging.getLogger(__name__)


def _migrate_monitoring_columns() -> None:
    inspector = inspect(engine)
    is_sqlite = str(engine.url).startswith("sqlite")

    if inspector.has_table("monitoring_settings"):
        cols = {c["name"] for c in inspector.get_columns("monitoring_settings")}
        with engine.begin() as conn:
            if "use_proxies" not in cols:
                default = "0" if is_sqlite else "FALSE"
                conn.execute(text(f"ALTER TABLE monitoring_settings ADD COLUMN use_proxies BOOLEAN DEFAULT {default}"))
            if "proxy_lines" not in cols:
                conn.execute(text("ALTER TABLE monitoring_settings ADD COLUMN proxy_lines TEXT DEFAULT ''"))
            if "discord_webhook_url" not in cols:
                conn.execute(text("ALTER TABLE monitoring_settings ADD COLUMN discord_webhook_url TEXT DEFAULT ''"))
            if "alerts_discord" not in cols:
                default = "1" if is_sqlite else "TRUE"
                conn.execute(text(f"ALTER TABLE monitoring_settings ADD COLUMN alerts_discord BOOLEAN DEFAULT {default}"))
            if "alerts_telegram" not in cols:
                default = "0" if is_sqlite else "FALSE"
                conn.execute(text(f"ALTER TABLE monitoring_settings ADD COLUMN alerts_telegram BOOLEAN DEFAULT {default}"))

    if inspector.has_table("alert_records"):
        alert_cols = {c["name"] for c in inspector.get_columns("alert_records")}
        with engine.begin() as conn:
            if "discord_ok" not in alert_cols:
                default = "0" if is_sqlite else "FALSE"
                conn.execute(text(f"ALTER TABLE alert_records ADD COLUMN discord_ok BOOLEAN DEFAULT {default}"))


def run_blocking_startup(settings: Settings) -> None:
    inspector = inspect(engine)
    if not inspector.has_table("users"):
        logger.info("Creating database tables...")
    Base.metadata.create_all(bind=engine)
    _migrate_monitoring_columns()
    db = SessionLocal()
    try:
        seed_database(db, settings.ADMIN_EMAIL.strip(), settings.ADMIN_PASSWORD)
        if settings.ADMIN_EMAIL.strip() and settings.ADMIN_PASSWORD:
            logger.info("Admin credentials synced from environment for %s", settings.ADMIN_EMAIL.strip().lower())
        ensure_visible_browser_setting(db)
        mon = db.query(MonitoringSetting).first()
        if mon:
            if settings.TELEGRAM_BOT_TOKEN and not mon.telegram_bot_token:
                mon.telegram_bot_token = settings.TELEGRAM_BOT_TOKEN
            if settings.TELEGRAM_CHAT_ID and not mon.telegram_chat_id:
                mon.telegram_chat_id = settings.TELEGRAM_CHAT_ID
            proxy_path = settings.data_dir / "proxy.txt"
            if proxy_path.is_file() and not (mon.proxy_lines or "").strip():
                mon.proxy_lines = proxy_path.read_text(encoding="utf-8").strip()
            if settings.DISCORD_WEBHOOK_URL and not (mon.discord_webhook_url or "").strip():
                mon.discord_webhook_url = settings.DISCORD_WEBHOOK_URL
            if mon.sitemap_scan_interval_sec == 45.0:
                mon.sitemap_scan_interval_sec = settings.SITEMAP_SCAN_INTERVAL_SEC
            # Migrate old 4–8s PDP poll defaults → 5–10s (max 15)
            if mon.poll_online_min < 5.0:
                mon.poll_online_min = settings.POLL_ONLINE_MIN
            if mon.poll_online_max < 5.0 or mon.poll_online_max > 15.0:
                mon.poll_online_max = min(15.0, max(settings.POLL_ONLINE_MAX, mon.poll_online_min))
            db.commit()
    finally:
        db.close()
