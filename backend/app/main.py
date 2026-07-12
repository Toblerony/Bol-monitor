import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import OperationalError, TimeoutError as SATimeoutError

from app.api import api_router
from app.config import get_settings
from app.database import check_database_connection, invalidate_pool
from app.startup_db import run_blocking_startup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()

DB_STARTUP_TIMEOUT_SECONDS = 90
DB_STARTUP_ATTEMPTS = 4


async def _connect_database_with_retries(app: FastAPI) -> bool:
    for attempt in range(1, DB_STARTUP_ATTEMPTS + 1):
        label = "initial" if attempt == 1 else f"retry {attempt - 1}/{DB_STARTUP_ATTEMPTS - 1}"
        logger.info(
            "Startup: database %s (%s, max %ss)...",
            label,
            settings.database_backend,
            DB_STARTUP_TIMEOUT_SECONDS,
        )
        try:
            await asyncio.wait_for(
                asyncio.to_thread(run_blocking_startup, settings),
                timeout=DB_STARTUP_TIMEOUT_SECONDS,
            )
            app.state.db_ready = True
            logger.info("Startup: database ready (%s)", settings.database_backend)
            return True
        except asyncio.TimeoutError:
            invalidate_pool()
            logger.warning("Startup: database timed out (%ss)", DB_STARTUP_TIMEOUT_SECONDS)
        except Exception as exc:
            invalidate_pool()
            logger.warning("Startup: database error: %s", exc)
        if attempt < DB_STARTUP_ATTEMPTS:
            wait = 3 * attempt
            logger.info("Startup: waiting %ss before next database attempt...", wait)
            await asyncio.sleep(wait)
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db_ready = False
    if not await _connect_database_with_retries(app):
        logger.error("Startup: database unavailable — API returns 503 until DB works")
    yield


app = FastAPI(title=settings.APP_NAME, version=settings.APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list + (["*"] if settings.DEBUG else []),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.exception_handler(OperationalError)
async def database_unavailable_handler(_request: Request, exc: OperationalError):
    invalidate_pool()
    logger.warning("Database unavailable: %s", exc)
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=503,
        content={"detail": "Database busy or offline — wait a moment and try again."},
    )


@app.get("/health")
def health(request: Request):
    db_ok = check_database_connection()
    db_ready = getattr(request.app.state, "db_ready", False)
    ready = db_ready and db_ok
    return {
        "status": "healthy" if ready else ("starting" if db_ready else "degraded"),
        "database": "connected" if db_ok else "offline",
        "database_backend": settings.database_backend,
        "ready": ready,
        "alive": True,
        "app": settings.APP_NAME,
    }
