import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, sa_enum
from app.models.log import ActivityLog, LogCategory, LogLevel
from app.models.settings import ApplicationSetting


class UserRole(str, enum.Enum):
    ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="Admin")
    role: Mapped[UserRole] = mapped_column(sa_enum(UserRole), default=UserRole.ADMIN, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProductStatus(str, enum.Enum):
    OFFLINE = "offline"
    ONLINE_OOS = "online_oos"
    IN_STOCK = "in_stock"
    UNKNOWN = "unknown"


class AlertType(str, enum.Enum):
    NEW_ONLINE = "new_online"
    IN_STOCK = "in_stock"


class ProductProfile(Base):
    __tablename__ = "product_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    title_keywords: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    category_keywords: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    exclude_keywords: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    price_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    tracked_products: Mapped[list["TrackedProduct"]] = relationship(back_populates="profile")


class TrackedProduct(Base):
    __tablename__ = "tracked_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("product_profiles.id"), index=True, nullable=False)
    url: Mapped[str] = mapped_column(String(1024), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    price_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    price_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    categories: Mapped[str] = mapped_column(Text, default="", nullable=False)
    brand: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    product_type: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    status: Mapped[ProductStatus] = mapped_column(
        sa_enum(ProductStatus), default=ProductStatus.UNKNOWN, nullable=False
    )
    alerted_online: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    alerted_stock: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    profile: Mapped[ProductProfile] = relationship(back_populates="tracked_products")


class AlertRecord(Base):
    __tablename__ = "alert_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    alert_type: Mapped[AlertType] = mapped_column(sa_enum(AlertType), nullable=False, index=True)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("product_profiles.id"), nullable=True)
    product_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    product_title: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    price_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_ok: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    discord_ok: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MonitoringSetting(Base):
    __tablename__ = "monitoring_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_running: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bol_session_ok: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    bol_session_message: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    sitemap_scan_interval_sec: Mapped[float] = mapped_column(Float, default=600.0, nullable=False)
    use_proxies: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    proxy_lines: Mapped[str] = mapped_column(Text, default="", nullable=False)
    poll_online_min: Mapped[float] = mapped_column(Float, default=4.0, nullable=False)
    poll_online_max: Mapped[float] = mapped_column(Float, default=8.0, nullable=False)
    poll_offline_min: Mapped[float] = mapped_column(Float, default=40.0, nullable=False)
    poll_offline_max: Mapped[float] = mapped_column(Float, default=60.0, nullable=False)
    alerts_new_online: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    alerts_in_stock: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    alerts_discord: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    alerts_telegram: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    discord_webhook_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    telegram_bot_token: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    telegram_chat_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)


