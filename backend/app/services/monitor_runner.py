"""24/7 monitoring loop: sitemap discovery + PDP polling + Discord/Telegram alerts."""
from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import AlertRecord, AlertType, LogCategory, MonitoringSetting, ProductProfile, ProductStatus, TrackedProduct
from app.services.bol_pdp_parser import fetch_product_page, product_status_string
from app.services.log_service import log_activity
from app.services.profile_matcher import matches_profile, profile_match_keywords_for_sitemap
from app.services.bol_session import (
    check_bol_session_still_valid_sync,
    get_bol_session_status,
    restore_session_file_from_db,
    SESSION_REQUIRED_MSG,
)
from app.services import sitemap_monitor as sm
from app.services.proxy_client import ProxyPool, parse_proxy_lines
from app.services.proxy_service import load_pool_from_db, sync_proxy_file
from app.services.alert_service import send_in_stock_alert, send_new_online_alert, send_session_logout_alert

SITEMAP_INTERVAL_MIN_SEC = 300.0  # 5 minutes
SITEMAP_INTERVAL_MAX_SEC = 900.0  # 15 minutes


class MonitorRunner:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._session_logout_alerted = False
        self._last_session_check = 0.0

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run_loop, name="bol-monitor", daemon=True)
            self._thread.start()
            log_activity("info", "Monitoring started", category=LogCategory.MONITORING, source="monitor_runner")

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._thread:
                self._thread.join(timeout=8.0)
                self._thread = None
        db = SessionLocal()
        try:
            mon = db.query(MonitoringSetting).first()
            if mon:
                mon.is_running = False
                db.commit()
        finally:
            db.close()
        log_activity("info", "Monitoring stopped", category=LogCategory.MONITORING, source="monitor_runner")

    def _settings(self, db: Session) -> MonitoringSetting:
        mon = db.query(MonitoringSetting).first()
        if not mon:
            mon = MonitoringSetting()
            db.add(mon)
            db.commit()
            db.refresh(mon)
        return mon

    def _proxy_pool(self, db: Session, mon: MonitoringSetting) -> ProxyPool | None:
        if not mon.use_proxies:
            return None
        pool = load_pool_from_db(db)
        if pool and pool.enabled:
            sync_proxy_file(mon.proxy_lines or "", get_settings().data_dir)
            return pool
        return None

    def _sitemap_proxy_path(self, mon: MonitoringSetting, settings) -> Path | None:
        if not mon.use_proxies or not parse_proxy_lines(mon.proxy_lines or ""):
            return None
        sync_proxy_file(mon.proxy_lines or "", settings.data_dir)
        return settings.data_dir / "proxy.txt"

    def _run_loop(self) -> None:
        status = get_bol_session_status()
        if not status["has_session"] or not status["has_database"]:
            log_activity(
                "error",
                status.get("message") or SESSION_REQUIRED_MSG,
                category=LogCategory.SYSTEM,
                source="monitor_runner",
            )
            db = SessionLocal()
            try:
                mon = self._settings(db)
                mon.is_running = False
                mon.is_enabled = False
                mon.bol_session_ok = False
                mon.bol_session_message = status.get("message") or SESSION_REQUIRED_MSG
                db.commit()
            finally:
                db.close()
            return
        if not restore_session_file_from_db():
            log_activity(
                "warning",
                "Could not restore Bol session file from database — using local file if present",
                category=LogCategory.SYSTEM,
                source="monitor_runner",
            )

        db = SessionLocal()
        try:
            mon = self._settings(db)
            mon.is_running = True
            mon.is_enabled = True
            db.commit()
        finally:
            db.close()

        last_sitemap = 0.0
        next_product_idx = 0
        session_check_interval = 600.0

        while not self._stop.is_set():
            db = SessionLocal()
            try:
                mon = self._settings(db)
                settings = get_settings()
                now = time.time()

                if now - self._last_session_check >= session_check_interval:
                    self._check_bol_session(db, mon, settings)
                    self._last_session_check = now

                interval = float(mon.sitemap_scan_interval_sec or settings.SITEMAP_SCAN_INTERVAL_SEC)
                interval = max(SITEMAP_INTERVAL_MIN_SEC, min(SITEMAP_INTERVAL_MAX_SEC, interval))

                if now - last_sitemap >= interval:
                    self._run_sitemap_scan(db, mon, settings)
                    mon.last_scan_at = datetime.now(timezone.utc)
                    db.commit()
                    last_sitemap = now

                products = (
                    db.query(TrackedProduct)
                    .join(ProductProfile)
                    .filter(ProductProfile.is_enabled == True)  # noqa: E712
                    .order_by(TrackedProduct.id)
                    .all()
                )
                if products:
                    tp = products[next_product_idx % len(products)]
                    next_product_idx += 1
                    delay = self._poll_one(db, mon, settings, tp)
                else:
                    delay = random.uniform(
                        float(mon.poll_online_min or settings.POLL_ONLINE_MIN),
                        float(mon.poll_online_max or settings.POLL_ONLINE_MAX),
                    )
                db.commit()
            except Exception as exc:
                log_activity("error", f"Monitor loop error: {exc}", category=LogCategory.ERROR, source="monitor_runner")
                delay = 5.0
            finally:
                db.close()

            if self._stop.wait(delay):
                break

        db = SessionLocal()
        try:
            mon = self._settings(db)
            mon.is_running = False
            db.commit()
        finally:
            db.close()

    def _check_bol_session(self, db: Session, mon: MonitoringSetting, settings) -> None:
        """Verify bol.com session — Telegram alert on logout (no auto-login)."""
        if check_bol_session_still_valid_sync(settings):
            self._session_logout_alerted = False
            mon.bol_session_ok = True
            mon.bol_session_message = "Bol session OK — saved in database"
            return

        mon.bol_session_ok = False
        mon.bol_session_message = "Bol session expired — run login-bol.bat on your PC"
        log_activity(
            "warning",
            "Bol session expired or logged out — run login-bol.bat (no auto-login)",
            category=LogCategory.SYSTEM,
            source="monitor_runner",
        )

        if self._session_logout_alerted:
            return

        tg_ok, dc_ok, err = send_session_logout_alert(mon, settings)
        self._session_logout_alerted = True
        channels = []
        if tg_ok:
            channels.append("Telegram")
        if dc_ok:
            channels.append("Discord")
        log_activity(
            "info" if channels else "error",
            f"SESSION EXPIRED alert sent ({', '.join(channels) or 'none'})"
            if channels
            else f"Session alert failed: {err or 'no channel configured'}",
            category=LogCategory.NOTIFICATION,
            source="alerts",
        )

    def _run_sitemap_scan(self, db: Session, mon: MonitoringSetting, settings) -> None:
        profiles = db.query(ProductProfile).filter(ProductProfile.is_enabled == True).all()  # noqa: E712
        keywords = profile_match_keywords_for_sitemap(profiles)
        if not keywords:
            log_activity(
                "warning",
                "Sitemap scan skipped — no profile keywords configured",
                category=LogCategory.SCRAPER,
                source="sitemap",
            )
            return

        data_dir = settings.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        proxy_path = self._sitemap_proxy_path(mon, settings)

        try:
            new_urls = sm.discover_new_urls_for_keywords(
                keywords,
                db_path=data_dir / "bol_sitemap.sqlite3",
                all_links_path=data_dir / "all_links.txt",
                new_links_path=data_dir / "newlinks.txt",
                proxy_file=proxy_path,
                workers=2,
                silent=True,
            )
        except Exception as exc:
            log_activity("error", f"Sitemap scan failed: {exc}", category=LogCategory.SCRAPER, source="sitemap")
            return

        log_activity(
            "info",
            f"Sitemap scan: {len(new_urls)} new candidate URL(s)",
            category=LogCategory.SCRAPER,
            source="sitemap",
        )
        pool = self._proxy_pool(db, mon)
        for url in new_urls:
            if self._stop.is_set():
                break
            existing = db.query(TrackedProduct).filter(TrackedProduct.url == url).first()
            if existing:
                continue
            data = fetch_product_page(url, pool=pool)
            matched = [p for p in profiles if matches_profile(p, data)]
            if not matched:
                continue
            profile = matched[0]
            status = ProductStatus(product_status_string(data))
            tp = TrackedProduct(
                profile_id=profile.id,
                url=url,
                title=data.title,
                price_text=data.price_text,
                price_value=data.price_value,
                categories=data.raw_categories_text,
                brand=data.brand,
                product_type=data.product_type,
                status=status,
                last_checked_at=datetime.now(timezone.utc),
            )
            db.add(tp)
            db.flush()
            if status != ProductStatus.OFFLINE:
                self._maybe_alert_online(db, mon, tp, profile)
            if status == ProductStatus.IN_STOCK:
                self._maybe_alert_stock(db, mon, tp, profile)
            log_activity(
                "info",
                f"Discovered: {data.title or url[:60]}",
                category=LogCategory.MONITORING,
                source="discovery",
            )

    def _poll_one(self, db: Session, mon: MonitoringSetting, settings, tp: TrackedProduct) -> float:
        pool = self._proxy_pool(db, mon)
        data = fetch_product_page(tp.url, pool=pool)
        profile = db.query(ProductProfile).filter(ProductProfile.id == tp.profile_id).first()
        if not profile or not profile.is_enabled:
            return random.uniform(8.0, 12.0)

        prev = tp.status
        new_status = ProductStatus(product_status_string(data))
        tp.title = data.title or tp.title
        tp.price_text = data.price_text or tp.price_text
        tp.price_value = data.price_value if data.price_value is not None else tp.price_value
        tp.categories = data.raw_categories_text or tp.categories
        tp.brand = data.brand or tp.brand
        tp.product_type = data.product_type or tp.product_type
        tp.status = new_status
        tp.last_checked_at = datetime.now(timezone.utc)

        if prev == ProductStatus.OFFLINE and new_status in (
            ProductStatus.ONLINE_OOS,
            ProductStatus.IN_STOCK,
            ProductStatus.UNKNOWN,
        ):
            self._maybe_alert_online(db, mon, tp, profile)

        if new_status == ProductStatus.IN_STOCK and prev != ProductStatus.IN_STOCK:
            self._maybe_alert_stock(db, mon, tp, profile)

        if new_status == ProductStatus.OFFLINE:
            lo = float(mon.poll_offline_min or settings.POLL_OFFLINE_MIN)
            hi = float(mon.poll_offline_max or settings.POLL_OFFLINE_MAX)
        else:
            lo = float(mon.poll_online_min or settings.POLL_ONLINE_MIN)
            hi = float(mon.poll_online_max or settings.POLL_ONLINE_MAX)
        return random.uniform(lo, hi)

    def _maybe_alert_online(
        self, db: Session, mon: MonitoringSetting, tp: TrackedProduct, profile: ProductProfile
    ) -> None:
        if tp.alerted_online or not mon.alerts_new_online:
            return
        tg_ok, dc_ok, err = send_new_online_alert(
            mon,
            title=tp.title,
            url=tp.url,
            price=tp.price_text,
            profile_name=profile.name,
        )
        tp.alerted_online = True
        db.add(
            AlertRecord(
                alert_type=AlertType.NEW_ONLINE,
                profile_id=profile.id,
                product_url=tp.url,
                product_title=tp.title,
                price_text=tp.price_text,
                telegram_ok=tg_ok,
                discord_ok=dc_ok,
                error_message=err or None,
            )
        )
        ok = tg_ok or dc_ok
        log_activity(
            "info" if ok else "error",
            f"NEW ONLINE: {tp.title or tp.url[:50]}"
            + (f" (Discord{' + Telegram' if tg_ok and dc_ok else ''})" if ok else f" — {err}"),
            category=LogCategory.NOTIFICATION,
            source="alerts",
        )

    def _maybe_alert_stock(
        self, db: Session, mon: MonitoringSetting, tp: TrackedProduct, profile: ProductProfile
    ) -> None:
        if tp.alerted_stock or not mon.alerts_in_stock:
            return
        tg_ok, dc_ok, err = send_in_stock_alert(
            mon,
            title=tp.title,
            url=tp.url,
            price=tp.price_text,
            profile_name=profile.name,
        )
        tp.alerted_stock = True
        db.add(
            AlertRecord(
                alert_type=AlertType.IN_STOCK,
                profile_id=profile.id,
                product_url=tp.url,
                product_title=tp.title,
                price_text=tp.price_text,
                telegram_ok=tg_ok,
                discord_ok=dc_ok,
                error_message=err or None,
            )
        )
        ok = tg_ok or dc_ok
        log_activity(
            "info" if ok else "error",
            f"IN STOCK: {tp.title or tp.url[:50]}"
            + ("" if ok else f" — {err}"),
            category=LogCategory.NOTIFICATION,
            source="alerts",
        )


monitor_runner = MonitorRunner()
