import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


def is_cloud_host() -> bool:
    """True on Render — monitoring runs headless on the server."""
    return bool(os.environ.get("RENDER", "").strip())


def resolve_sqlite_database_url(url: str) -> str:
    if not url.startswith("sqlite"):
        return url
    if url.startswith("sqlite:////"):
        return url
    path_part = url.removeprefix("sqlite:///")
    if path_part.startswith("./"):
        path_part = path_part[2:]
    db_path = (_BACKEND_ROOT / path_part).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path.as_posix()}"


def normalize_database_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        raise ValueError(
            "DATABASE_URL is empty — paste your Neon connection string in backend/.env "
            "(same URL on PC and Render; login-bol.bat saves session to this DB)."
        )
    if u.startswith("sqlite"):
        return resolve_sqlite_database_url(u)
    if u.startswith("postgres://"):
        u = "postgresql+psycopg://" + u[len("postgres://") :]
    elif u.startswith("postgresql://") and "+psycopg" not in u and "+psycopg2" not in u:
        u = "postgresql+psycopg://" + u[len("postgresql://") :]
    if u.startswith("postgresql") and "sslmode=" not in u:
        sep = "&" if "?" in u else "?"
        u = f"{u}{sep}sslmode=require"
    return u


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    APP_NAME: str = "Bol Monitor"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # Set in backend/.env — Neon URL required (same on PC + Render for login-bol.bat session sync)
    DATABASE_URL: str = ""
    API_PORT: int = 8003

    SECRET_KEY: str = "change-this-secret-key-in-production-use-long-random-string"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    CORS_ORIGINS: str = "http://localhost:5175,http://127.0.0.1:5175"

    ADMIN_EMAIL: str = ""
    ADMIN_PASSWORD: str = ""

    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    DISCORD_WEBHOOK_URL: str = ""

    USE_PROXIES: bool = False
    SITEMAP_SCAN_INTERVAL_SEC: float = 600.0
    POLL_ONLINE_MIN: float = 5.0
    POLL_ONLINE_MAX: float = 10.0
    POLL_OFFLINE_MIN: float = 40.0
    POLL_OFFLINE_MAX: float = 60.0

    PLAYWRIGHT_TIMEOUT: int = 60000
    PLAYWRIGHT_HEADLESS: bool | None = None

    BOL_SESSION_FILE: str = "data/bol_session.json"
    BOL_PROFILE_DIR: str = "data/bol_chrome_profile"
    SITEMAP_DB: str = "data/bol_sitemap.sqlite3"

    @property
    def database_url_resolved(self) -> str:
        return normalize_database_url(self.DATABASE_URL)

    @property
    def database_backend(self) -> str:
        url = self.database_url_resolved
        if url.startswith("sqlite"):
            return "sqlite"
        if url.startswith("postgresql"):
            return "postgresql"
        return "unknown"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def data_dir(self) -> Path:
        return _BACKEND_ROOT / "data"

    @property
    def session_file(self) -> Path:
        return _BACKEND_ROOT / self.BOL_SESSION_FILE

    @property
    def profile_dir(self) -> Path:
        return _BACKEND_ROOT / self.BOL_PROFILE_DIR


@lru_cache
def get_settings() -> Settings:
    return Settings()
