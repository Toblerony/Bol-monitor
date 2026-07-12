@echo off
cd /d "%~dp0\backend"
call venv\Scripts\activate
python scripts\ensure_playwright_chromium.py
pause
