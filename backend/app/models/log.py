import enum
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, sa_enum


class LogLevel(str, enum.Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LogCategory(str, enum.Enum):
    MONITORING = "monitoring"
    SCRAPER = "scraper"
    NOTIFICATION = "notification"
    SYSTEM = "system"
    ERROR = "error"


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    category: Mapped[LogCategory] = mapped_column(sa_enum(LogCategory), nullable=False, index=True)
    level: Mapped[LogLevel] = mapped_column(sa_enum(LogLevel), default=LogLevel.INFO, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
