"""Load proxy pool from monitoring settings."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import MonitoringSetting
from app.services.proxy_client import ProxyPool, get_proxy_pool, invalidate_proxy_pool


def load_pool_from_db(db: Session) -> ProxyPool | None:
    mon = db.query(MonitoringSetting).first()
    if not mon:
        return None
    return get_proxy_pool(mon.proxy_lines or "", bool(mon.use_proxies))


def reload_pool(proxy_lines: str, use_proxies: bool) -> ProxyPool | None:
    invalidate_proxy_pool()
    return get_proxy_pool(proxy_lines, use_proxies)


def sync_proxy_file(proxy_lines: str, data_dir) -> None:
    """Keep backend/data/proxy.txt in sync for sitemap_monitor."""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "proxy.txt").write_text(proxy_lines or "", encoding="utf-8")
