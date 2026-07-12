"""Seed admin user and default monitoring row."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.security import get_password_hash
from app.models import MonitoringSetting, User, UserRole


def seed_database(db: Session, admin_email: str, admin_password: str) -> None:
    email = (admin_email or "").strip().lower()
    if email and admin_password:
        existing = db.query(User).filter(User.email == email).first()
        if not existing:
            db.add(
                User(
                    email=email,
                    hashed_password=get_password_hash(admin_password),
                    full_name="Admin",
                    role=UserRole.ADMIN,
                    is_primary=True,
                )
            )
    if not db.query(MonitoringSetting).first():
        db.add(MonitoringSetting())
    db.commit()
