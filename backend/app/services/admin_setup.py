"""First-run admin setup."""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def is_admin_setup_needed(db: Session) -> bool:
    if db.query(User).filter(User.is_primary == True).first():  # noqa: E712
        return False
    settings = get_settings()
    return not (settings.ADMIN_EMAIL.strip() and settings.ADMIN_PASSWORD.strip())


def persist_admin_env(email: str, password: str) -> None:
    lines: list[str] = []
    if _ENV_PATH.exists():
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()
    keys = {"ADMIN_EMAIL": email.strip(), "ADMIN_PASSWORD": password}
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if "=" in line and not line.strip().startswith("#"):
            k = line.split("=", 1)[0].strip()
            if k in keys:
                out.append(f"{k}={keys[k]}")
                seen.add(k)
                continue
        out.append(line)
    for k, v in keys.items():
        if k not in seen:
            out.append(f"{k}={v}")
    _ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ["ADMIN_EMAIL"] = email.strip()
    os.environ["ADMIN_PASSWORD"] = password


def reload_settings() -> None:
    get_settings.cache_clear()
