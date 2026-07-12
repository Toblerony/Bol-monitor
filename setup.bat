@echo off
echo ========================================
echo  Bol Monitor - Full Setup
echo  Neon DB + Python + Frontend
echo ========================================
cd /d "%~dp0"

echo.
echo [1/4] Python virtual environment...
cd backend
if not exist "venv" (
    python -m venv venv
)
call venv\Scripts\activate
if not exist ".env" copy .env.example .env
if exist "data\proxy.txt" del /f /q "data\proxy.txt" >nul 2>&1
echo Installing Python packages...
pip install -r requirements.txt
if errorlevel 1 (
    echo Python install failed.
    pause
    exit /b 1
)

echo.
echo [2/4] Database — paste Neon DATABASE_URL in backend\.env first...
if not exist "data" mkdir data
python -c "from app.config import get_settings; from app.startup_db import run_blocking_startup; s=get_settings(); print('DB backend:', s.database_backend); run_blocking_startup(s); print('Database ready')"
if errorlevel 1 (
    echo Database init failed. Check DATABASE_URL in backend\.env
    pause
    exit /b 1
)

echo.
echo [3/4] Frontend npm packages...
cd ..\frontend
call npm install
if errorlevel 1 (
    echo npm install failed.
    pause
    exit /b 1
)

echo.
echo [4/4] Playwright Chromium (~180 MB, required for login-bol.bat)...
cd ..\backend
set PLAYWRIGHT_BROWSERS_PATH=%~dp0backend\playwright-browsers
python scripts\ensure_playwright_chromium.py
if errorlevel 1 (
    echo Chromium install failed. Check internet and run install-chromium.bat
    pause
    exit /b 1
)

echo.
echo ========================================
echo  Setup complete!
echo.
echo  1. Paste Neon DATABASE_URL in backend\.env (same URL on Render)
echo  2. Double-click login-bol.bat — log in to bol.com once
echo  3. Double-click startall.bat
echo  4. Open http://localhost:5175 — add profiles, Telegram, Start
echo ========================================
pause
