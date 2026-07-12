from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field


class AdminSetupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)


class SetupStatusResponse(BaseModel):
    needs_setup: bool


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: int
    email: str
    full_name: str
    role: str

    class Config:
        from_attributes = True


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    title_keywords: list[str] = Field(default_factory=list)
    category_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    price_min: float | None = None
    price_max: float | None = None
    is_enabled: bool = True


class ProfileUpdate(BaseModel):
    name: str | None = None
    title_keywords: list[str] | None = None
    category_keywords: list[str] | None = None
    exclude_keywords: list[str] | None = None
    price_min: float | None = None
    price_max: float | None = None
    is_enabled: bool | None = None


class ProfileResponse(BaseModel):
    id: int
    name: str
    title_keywords: list[str]
    category_keywords: list[str]
    exclude_keywords: list[str]
    price_min: float | None
    price_max: float | None
    is_enabled: bool
    tracked_count: int = 0

    class Config:
        from_attributes = True


class TrackedProductResponse(BaseModel):
    id: int
    profile_id: int
    profile_name: str = ""
    url: str
    title: str
    price_text: str | None
    status: str
    categories: str
    brand: str
    product_type: str
    alerted_online: bool
    alerted_stock: bool
    last_checked_at: datetime | None

    class Config:
        from_attributes = True


class AlertResponse(BaseModel):
    id: int
    alert_type: str
    product_url: str
    product_title: str
    price_text: str | None
    telegram_ok: bool
    discord_ok: bool
    sent_at: datetime

    class Config:
        from_attributes = True


class MonitoringSettingsResponse(BaseModel):
    is_enabled: bool
    is_running: bool
    last_scan_at: datetime | None
    bol_session_ok: bool
    bol_session_message: str
    sitemap_scan_interval_sec: float
    poll_online_min: float
    poll_online_max: float
    poll_offline_min: float
    poll_offline_max: float
    alerts_new_online: bool
    alerts_in_stock: bool
    alerts_discord: bool = True
    alerts_telegram: bool = False
    discord_webhook_url: str = ""
    telegram_bot_token: str
    telegram_chat_id: str
    use_proxies: bool = False
    proxy_count: int = 0
    tracked_count: int = 0
    profile_count: int = 0


class MonitoringSettingsUpdate(BaseModel):
    is_enabled: bool | None = None
    sitemap_scan_interval_sec: float | None = None
    poll_online_min: float | None = None
    poll_online_max: float | None = None
    poll_offline_min: float | None = None
    poll_offline_max: float | None = None
    alerts_new_online: bool | None = None
    alerts_in_stock: bool | None = None
    alerts_discord: bool | None = None
    alerts_telegram: bool | None = None
    discord_webhook_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


class ProxySettingsResponse(BaseModel):
    use_proxies: bool
    proxy_lines: str
    proxy_count: int


class ProxySettingsUpdate(BaseModel):
    use_proxies: bool | None = None
    proxy_lines: str | None = None


class ProxyTestResult(BaseModel):
    proxy: str
    ok: bool
    message: str


class ProxyTestResponse(BaseModel):
    results: list[ProxyTestResult]


class DashboardStats(BaseModel):
    is_running: bool
    bol_session_ok: bool
    profiles_enabled: int
    tracked_products: int
    alerts_today: int
    by_status: dict[str, int]


class LogEntryResponse(BaseModel):
    id: int
    category: str
    level: str
    message: str
    details: str | None = None
    source: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class LogsBulkDelete(BaseModel):
    ids: list[int]


class ChartDataPoint(BaseModel):
    date: str
    count: int


class DashboardCharts(BaseModel):
    products_per_day: list[ChartDataPoint]
    alerts_per_day: list[ChartDataPoint]
    online_alerts_per_day: list[ChartDataPoint]


class BolLoginStatusResponse(BaseModel):
    logged_in: bool
    has_session: bool
    has_file: bool
    has_database: bool
    message: str


class TestTelegramResponse(BaseModel):
    ok: bool
    message: str


class TestDiscordResponse(BaseModel):
    ok: bool
    message: str
