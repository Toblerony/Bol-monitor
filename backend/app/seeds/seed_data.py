"""Seed / sync admin user and default monitoring row."""
from __future__ import annotations

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.security import get_password_hash, verify_password
from app.models import MonitoringSetting, User, UserRole

logger = logging.getLogger(__name__)


def ensure_admin_user(db: Session, admin_email: str, admin_password: str) -> User | None:
    """
    Create or sync the primary admin from this process's env credentials.

    Local uses backend/.env; Render uses dashboard env vars.
    Each environment's ADMIN_EMAIL / ADMIN_PASSWORD always work against that backend.
    """
    email = (admin_email or "").strip().lower()
    password = admin_password or ""
    if not email or not password:
        return None

    user = db.query(User).filter(func.lower(User.email) == email).first()
    if user is None:
        user = User(
            email=email,
            hashed_password=get_password_hash(password),
            full_name="Admin",
            role=UserRole.ADMIN,
            is_primary=True,
            is_active=True,
        )
        db.add(user)
        logger.info("Admin user created from env: %s", email)
    else:
        if not verify_password(password, user.hashed_password):
            user.hashed_password = get_password_hash(password)
            logger.info("Admin password synced from env for: %s", email)
        user.is_primary = True
        user.is_active = True
        user.role = UserRole.ADMIN
        if user.email != email:
            user.email = email

    # Only one primary admin
    for other in db.query(User).filter(User.is_primary == True, User.id != user.id).all():  # noqa: E712
        other.is_primary = False

    if not db.query(MonitoringSetting).first():
        db.add(MonitoringSetting())
    db.commit()
    db.refresh(user)
    return user


def seed_database(db: Session, admin_email: str, admin_password: str) -> None:
    """Back-compat name used by startup + setup routes."""
    ensure_admin_user(db, admin_email, admin_password)
    if not db.query(MonitoringSetting).first():
        db.add(MonitoringSetting())
        db.commit()
