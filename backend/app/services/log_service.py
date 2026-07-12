"""Structured activity logs — monitoring, scraper, notifications, system."""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import ActivityLog, LogCategory, LogLevel

logger = logging.getLogger(__name__)

_LEVEL_MAP = {
    "debug": LogLevel.DEBUG,
    "info": LogLevel.INFO,
    "warning": LogLevel.WARNING,
    "warn": LogLevel.WARNING,
    "error": LogLevel.ERROR,
}


def _coerce_level(level: str | LogLevel) -> LogLevel:
    if isinstance(level, LogLevel):
        return level
    return _LEVEL_MAP.get(str(level).lower(), LogLevel.INFO)


def _coerce_category(category: str | LogCategory | None) -> LogCategory:
    if isinstance(category, LogCategory):
        return category
    if category:
        try:
            return LogCategory(str(category).lower())
        except ValueError:
            pass
    return LogCategory.MONITORING


def log_activity(
    level: str | LogLevel,
    message: str,
    *,
    category: str | LogCategory = LogCategory.MONITORING,
    details: dict | None = None,
    source: str | None = None,
    persist: bool = True,
) -> ActivityLog | None:
    if not persist:
        return None
    db = SessionLocal()
    try:
        entry = ActivityLog(
            category=_coerce_category(category),
            level=_coerce_level(level),
            message=message,
            details=json.dumps(details) if details else None,
            source=source,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry
    except Exception as exc:
        db.rollback()
        logger.warning("Activity log write failed: %s", exc)
        return None
    finally:
        db.close()


def log_activity_db(
    db: Session,
    category: LogCategory,
    message: str,
    level: LogLevel = LogLevel.INFO,
    details: dict | None = None,
    source: str | None = None,
) -> ActivityLog:
    entry = ActivityLog(
        category=category,
        level=level,
        message=message,
        details=json.dumps(details) if details else None,
        source=source,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def log_activity_isolated(
    category: LogCategory,
    message: str,
    level: LogLevel = LogLevel.INFO,
    details: dict | None = None,
    source: str | None = None,
) -> None:
    db = SessionLocal()
    try:
        log_activity_db(db, category, message, level=level, details=details, source=source)
    except Exception as exc:
        logger.warning("Activity log write failed: %s", exc)
    finally:
        db.close()
