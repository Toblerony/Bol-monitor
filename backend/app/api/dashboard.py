from datetime import datetime, timedelta, timezone
import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, require_db_ready
from app.database import get_db
from app.models import ActivityLog, AlertRecord, AlertType, MonitoringSetting, ProductProfile, TrackedProduct, User
from app.schemas import (
    AlertResponse,
    BolLoginStatusResponse,
    ChartDataPoint,
    DashboardCharts,
    DashboardStats,
    LogEntryResponse,
    LogsBulkDelete,
    MonitoringSettingsResponse,
    MonitoringSettingsUpdate,
    ProfileCreate,
    ProfileResponse,
    ProfileUpdate,
    ProxySettingsResponse,
    ProxySettingsUpdate,
    ProxyTestResponse,
    ProxyTestResult,
    TestDiscordResponse,
    TestTelegramResponse,
    TrackedProductResponse,
)
from app.config import get_settings
from app.services.monitor_runner import monitor_runner
from app.services.profile_utils import dumps_keywords, profile_to_response
from app.services.proxy_client import parse_proxy_lines, test_proxy
from app.services.proxy_service import reload_pool, sync_proxy_file
from app.services.telegram_service import send_telegram_message
from app.services.discord_service import send_discord_message_sync
from app.services.bol_session import (
    assert_bol_session_for_start,
    clear_bol_browser_data,
    get_bol_session_status,
    session_status_message,
    update_monitoring_session_flag,
)

router = APIRouter(tags=["Dashboard"])


def _mon(db: Session) -> MonitoringSetting:
    m = db.query(MonitoringSetting).first()
    if not m:
        m = MonitoringSetting()
        db.add(m)
        db.commit()
        db.refresh(m)
    return m


@router.get("/dashboard/stats", response_model=DashboardStats)
def dashboard_stats(
    _: None = Depends(require_db_ready),
    db: Session = Depends(get_db),
    __: User = Depends(get_current_user),
):
    from app.models import ProductStatus

    mon = _mon(db)
    update_monitoring_session_flag(db)
    db.commit()
    db.refresh(mon)
    profiles = db.query(ProductProfile).filter(ProductProfile.is_enabled == True).count()  # noqa: E712
    tracked = db.query(TrackedProduct).count()
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    alerts_today = db.query(AlertRecord).filter(AlertRecord.sent_at >= today).count()
    status_counts: dict[str, int] = {s.value: 0 for s in ProductStatus}
    for (st,) in db.query(TrackedProduct.status).all():
        key = st.value if hasattr(st, "value") else str(st)
        status_counts[key] = status_counts.get(key, 0) + 1
    return DashboardStats(
        is_running=mon.is_running,
        bol_session_ok=mon.bol_session_ok,
        profiles_enabled=profiles,
        tracked_products=tracked,
        alerts_today=alerts_today,
        by_status=status_counts,
    )


@router.get("/dashboard/charts", response_model=DashboardCharts)
def get_dashboard_charts(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    days = 14
    now = datetime.now(timezone.utc)
    products_per_day: list[ChartDataPoint] = []
    alerts_per_day: list[ChartDataPoint] = []
    online_alerts_per_day: list[ChartDataPoint] = []

    for i in range(days - 1, -1, -1):
        day = (now - timedelta(days=i)).date()
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)

        products_count = db.query(TrackedProduct).filter(
            TrackedProduct.created_at >= day_start,
            TrackedProduct.created_at < day_end,
        ).count()
        alerts_count = db.query(AlertRecord).filter(
            AlertRecord.sent_at >= day_start,
            AlertRecord.sent_at < day_end,
        ).count()
        online_count = db.query(AlertRecord).filter(
            AlertRecord.sent_at >= day_start,
            AlertRecord.sent_at < day_end,
            AlertRecord.alert_type == AlertType.NEW_ONLINE,
        ).count()

        date_str = day.isoformat()
        products_per_day.append(ChartDataPoint(date=date_str, count=products_count))
        alerts_per_day.append(ChartDataPoint(date=date_str, count=alerts_count))
        online_alerts_per_day.append(ChartDataPoint(date=date_str, count=online_count))

    return DashboardCharts(
        products_per_day=products_per_day,
        alerts_per_day=alerts_per_day,
        online_alerts_per_day=online_alerts_per_day,
    )


@router.get("/monitoring/settings", response_model=MonitoringSettingsResponse)
def get_monitoring_settings(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    mon = _mon(db)
    update_monitoring_session_flag(db)
    db.commit()
    db.refresh(mon)
    return MonitoringSettingsResponse(
        is_enabled=mon.is_enabled,
        is_running=mon.is_running,
        last_scan_at=mon.last_scan_at,
        bol_session_ok=mon.bol_session_ok,
        bol_session_message=mon.bol_session_message,
        sitemap_scan_interval_sec=mon.sitemap_scan_interval_sec,
        poll_online_min=mon.poll_online_min,
        poll_online_max=mon.poll_online_max,
        poll_offline_min=mon.poll_offline_min,
        poll_offline_max=mon.poll_offline_max,
        alerts_new_online=mon.alerts_new_online,
        alerts_in_stock=mon.alerts_in_stock,
        alerts_discord=mon.alerts_discord,
        alerts_telegram=mon.alerts_telegram,
        discord_webhook_url=mon.discord_webhook_url,
        telegram_bot_token=mon.telegram_bot_token,
        telegram_chat_id=mon.telegram_chat_id,
        use_proxies=mon.use_proxies,
        proxy_count=len(parse_proxy_lines(mon.proxy_lines or "")),
        tracked_count=db.query(TrackedProduct).count(),
        profile_count=db.query(ProductProfile).count(),
    )


@router.put("/monitoring/settings", response_model=MonitoringSettingsResponse)
def update_monitoring_settings(
    data: MonitoringSettingsUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    mon = _mon(db)
    for field in data.model_fields:
        val = getattr(data, field)
        if val is not None:
            setattr(mon, field, val)
    db.commit()
    db.refresh(mon)
    return MonitoringSettingsResponse(
        is_enabled=mon.is_enabled,
        is_running=mon.is_running,
        last_scan_at=mon.last_scan_at,
        bol_session_ok=mon.bol_session_ok,
        bol_session_message=mon.bol_session_message,
        sitemap_scan_interval_sec=mon.sitemap_scan_interval_sec,
        poll_online_min=mon.poll_online_min,
        poll_online_max=mon.poll_online_max,
        poll_offline_min=mon.poll_offline_min,
        poll_offline_max=mon.poll_offline_max,
        alerts_new_online=mon.alerts_new_online,
        alerts_in_stock=mon.alerts_in_stock,
        alerts_discord=mon.alerts_discord,
        alerts_telegram=mon.alerts_telegram,
        discord_webhook_url=mon.discord_webhook_url,
        telegram_bot_token=mon.telegram_bot_token,
        telegram_chat_id=mon.telegram_chat_id,
        use_proxies=mon.use_proxies,
        proxy_count=len(parse_proxy_lines(mon.proxy_lines or "")),
        tracked_count=db.query(TrackedProduct).count(),
        profile_count=db.query(ProductProfile).count(),
    )


@router.post("/monitoring/start")
def start_monitoring(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    mon = _mon(db)
    assert_bol_session_for_start()
    profiles = db.query(ProductProfile).filter(ProductProfile.is_enabled == True).count()  # noqa: E712
    if profiles == 0:
        raise HTTPException(status_code=400, detail="Add at least one enabled product profile first")
    if mon.use_proxies and not parse_proxy_lines(mon.proxy_lines or ""):
        raise HTTPException(
            status_code=400,
            detail="Proxies enabled but none configured — add proxies in Settings → Proxies or disable proxies",
        )
    update_monitoring_session_flag(db)
    mon.is_enabled = True
    db.commit()
    monitor_runner.start()
    return {"message": "Monitoring started", "is_running": True, "bol_login": mon.bol_session_message}


@router.post("/monitoring/stop")
def stop_monitoring(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    monitor_runner.stop()
    mon = _mon(db)
    mon.is_enabled = False
    db.commit()
    return {"message": "Monitoring stopped", "is_running": False}


@router.get("/profiles", response_model=list[ProfileResponse])
def list_profiles(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    out = []
    for p in db.query(ProductProfile).order_by(ProductProfile.id).all():
        cnt = db.query(TrackedProduct).filter(TrackedProduct.profile_id == p.id).count()
        out.append(ProfileResponse(**profile_to_response(p, cnt)))
    return out


@router.post("/profiles", response_model=ProfileResponse)
def create_profile(data: ProfileCreate, _: User = Depends(get_current_user), db: Session = Depends(get_db)):
    p = ProductProfile(
        name=data.name.strip(),
        title_keywords=dumps_keywords(data.title_keywords),
        category_keywords=dumps_keywords(data.category_keywords),
        exclude_keywords=dumps_keywords(data.exclude_keywords),
        price_min=data.price_min,
        price_max=data.price_max,
        is_enabled=data.is_enabled,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return ProfileResponse(**profile_to_response(p, 0))


@router.put("/profiles/{profile_id}", response_model=ProfileResponse)
def update_profile(
    profile_id: int,
    data: ProfileUpdate,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p = db.query(ProductProfile).filter(ProductProfile.id == profile_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    if data.name is not None:
        p.name = data.name.strip()
    if data.title_keywords is not None:
        p.title_keywords = dumps_keywords(data.title_keywords)
    if data.category_keywords is not None:
        p.category_keywords = dumps_keywords(data.category_keywords)
    if data.exclude_keywords is not None:
        p.exclude_keywords = dumps_keywords(data.exclude_keywords)
    if data.price_min is not None:
        p.price_min = data.price_min
    if data.price_max is not None:
        p.price_max = data.price_max
    if data.is_enabled is not None:
        p.is_enabled = data.is_enabled
    db.commit()
    db.refresh(p)
    cnt = db.query(TrackedProduct).filter(TrackedProduct.profile_id == p.id).count()
    return ProfileResponse(**profile_to_response(p, cnt))


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: int, _: User = Depends(get_current_user), db: Session = Depends(get_db)):
    p = db.query(ProductProfile).filter(ProductProfile.id == profile_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    db.query(TrackedProduct).filter(TrackedProduct.profile_id == profile_id).delete()
    db.delete(p)
    db.commit()
    return {"message": "Deleted"}


@router.get("/products", response_model=list[TrackedProductResponse])
def list_tracked_products(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    out = []
    for tp in db.query(TrackedProduct).order_by(TrackedProduct.id.desc()).limit(500).all():
        prof = db.query(ProductProfile).filter(ProductProfile.id == tp.profile_id).first()
        out.append(
            TrackedProductResponse(
                id=tp.id,
                profile_id=tp.profile_id,
                profile_name=prof.name if prof else "",
                url=tp.url,
                title=tp.title,
                price_text=tp.price_text,
                status=tp.status.value,
                categories=tp.categories,
                brand=tp.brand,
                product_type=tp.product_type,
                alerted_online=tp.alerted_online,
                alerted_stock=tp.alerted_stock,
                last_checked_at=tp.last_checked_at,
            )
        )
    return out


@router.get("/alerts", response_model=list[AlertResponse])
def list_alerts(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(AlertRecord).order_by(AlertRecord.id.desc()).limit(100).all()
    return [
        AlertResponse(
            id=r.id,
            alert_type=r.alert_type.value,
            product_url=r.product_url,
            product_title=r.product_title,
            price_text=r.price_text,
            telegram_ok=r.telegram_ok,
            discord_ok=r.discord_ok,
            sent_at=r.sent_at,
        )
        for r in rows
    ]


@router.get("/logs")
def list_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    category: str | None = None,
    level: str | None = None,
    search: str | None = None,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(ActivityLog).order_by(desc(ActivityLog.created_at))
    if category:
        query = query.filter(ActivityLog.category == category)
    if level:
        query = query.filter(ActivityLog.level == level)
    if search:
        query = query.filter(ActivityLog.message.ilike(f"%{search}%"))

    total = query.count()
    logs = query.offset((page - 1) * page_size).limit(page_size).all()
    return {
        "items": [
            LogEntryResponse(
                id=log.id,
                category=log.category.value,
                level=log.level.value,
                message=log.message,
                details=log.details,
                source=log.source,
                created_at=log.created_at,
            )
            for log in logs
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.delete("/logs/all")
def delete_all_logs(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    deleted = db.query(ActivityLog).delete(synchronize_session=False)
    db.commit()
    return {"deleted": deleted, "message": f"Deleted {deleted} logs"}


@router.delete("/logs")
def delete_logs(
    data: LogsBulkDelete,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not data.ids:
        raise HTTPException(status_code=400, detail="No log IDs provided")
    deleted = db.query(ActivityLog).filter(ActivityLog.id.in_(data.ids)).delete(synchronize_session=False)
    db.commit()
    return {"deleted": deleted, "message": f"Deleted {deleted} logs"}


@router.get("/logs/export/csv")
def export_logs_csv(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    logs = db.query(ActivityLog).order_by(desc(ActivityLog.created_at)).limit(5000).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Category", "Level", "Message", "Source", "Created At"])
    for log in logs:
        writer.writerow([
            log.id,
            log.category.value,
            log.level.value,
            log.message,
            log.source or "",
            log.created_at.isoformat(),
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=bol-logs.csv"},
    )


@router.get("/bol/login-status", response_model=BolLoginStatusResponse)
def bol_login_status(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    status = get_bol_session_status()
    ok, msg = session_status_message()
    mon = _mon(db)
    mon.bol_session_ok = ok
    mon.bol_session_message = msg
    db.commit()
    return BolLoginStatusResponse(**{**status, "message": msg, "logged_in": ok})


@router.post("/bol/clear-session")
def clear_bol_session(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Delete Bol session file + DB row — run login-bol.bat again."""
    monitor_runner.stop()
    mon = _mon(db)
    mon.is_enabled = False
    mon.is_running = False
    db.commit()
    result = clear_bol_browser_data()
    update_monitoring_session_flag(db)
    db.commit()
    return {
        "message": "Bol session cleared — run login-bol.bat on your PC, then Start monitoring",
        "cleared": result["session_file"] or result["profile_dir"],
        "details": result,
    }


@router.post("/telegram/test", response_model=TestTelegramResponse)
async def test_telegram(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    mon = _mon(db)
    token = mon.telegram_bot_token or get_settings().TELEGRAM_BOT_TOKEN
    chat = mon.telegram_chat_id or get_settings().TELEGRAM_CHAT_ID
    ok, err = await send_telegram_message(token, chat, "✅ Bol Monitor — Telegram test OK")
    return TestTelegramResponse(ok=ok, message=err or "Sent")


@router.post("/discord/test", response_model=TestDiscordResponse)
def test_discord(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    mon = _mon(db)
    webhook = mon.discord_webhook_url or get_settings().DISCORD_WEBHOOK_URL
    ok, err = send_discord_message_sync(
        webhook,
        "✅ **Bol Monitor** — Discord test OK",
        embed={"title": "Test alert", "description": "Webhook is working.", "color": 5763719},
    )
    return TestDiscordResponse(ok=ok, message=err or "Sent")


@router.get("/proxies", response_model=ProxySettingsResponse)
def get_proxies(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    mon = _mon(db)
    lines = mon.proxy_lines or ""
    return ProxySettingsResponse(
        use_proxies=mon.use_proxies,
        proxy_lines=lines,
        proxy_count=len(parse_proxy_lines(lines)),
    )


@router.put("/proxies", response_model=ProxySettingsResponse)
def update_proxies(data: ProxySettingsUpdate, _: User = Depends(get_current_user), db: Session = Depends(get_db)):
    mon = _mon(db)
    if data.use_proxies is not None:
        mon.use_proxies = data.use_proxies
    if data.proxy_lines is not None:
        mon.proxy_lines = data.proxy_lines.strip()
    db.commit()
    reload_pool(mon.proxy_lines or "", bool(mon.use_proxies))
    sync_proxy_file(mon.proxy_lines or "", get_settings().data_dir)
    lines = mon.proxy_lines or ""
    return ProxySettingsResponse(
        use_proxies=mon.use_proxies,
        proxy_lines=lines,
        proxy_count=len(parse_proxy_lines(lines)),
    )


@router.post("/proxies/test", response_model=ProxyTestResponse)
def test_proxies(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    mon = _mon(db)
    proxies = parse_proxy_lines(mon.proxy_lines or "")
    if not proxies:
        return ProxyTestResponse(results=[])
    results = []
    for p in proxies:
        ok, msg = test_proxy(p)
        results.append(ProxyTestResult(proxy=p.label or p.host, ok=ok, message=msg))
    return ProxyTestResponse(results=results)
