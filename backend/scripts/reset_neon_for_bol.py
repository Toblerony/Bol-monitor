"""Wipe Neon database — remove Facebook monitoring (and any old) data; create Bol tables only."""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from sqlalchemy import inspect, text

from app.config import get_settings
from app.database import Base, engine
from app.startup_db import run_blocking_startup


def main() -> int:
    settings = get_settings()
    print("=" * 60)
    print("RESET Neon database for Bol Monitor ONLY")
    print(f"Backend: {settings.database_backend}")
    print("This deletes ALL existing tables (Facebook monitoring, etc.)")
    print("=" * 60)

    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.execute(text("GRANT ALL ON SCHEMA public TO PUBLIC"))
        conn.commit()
        print("Dropped public schema and recreated (empty).")

    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    tables = sorted(inspector.get_table_names())
    print("Bol tables:", ", ".join(tables))

    run_blocking_startup(settings)
    print("\nSUCCESS — Neon DB is now Bol-only. Run setup/login if needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
