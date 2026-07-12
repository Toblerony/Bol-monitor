"""Server entry — python run.py"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

from app.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    # Render sets PORT; bind 0.0.0.0 so the platform can reach the service
    on_render = bool(os.environ.get("RENDER", "").strip())
    port = int(os.environ.get("PORT") or settings.API_PORT)
    host = "0.0.0.0" if on_render else "127.0.0.1"
    Path("data").mkdir(parents=True, exist_ok=True)
    print(f"Starting Bol Monitor backend on http://{host}:{port}")
    uvicorn.run("app.main:app", host=host, port=port, reload=False, log_level="info")
