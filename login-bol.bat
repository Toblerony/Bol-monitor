@echo off
echo ========================================
echo  Bol Login (one time / when logged out)
echo ========================================
cd /d "%~dp0"
if not exist "backend\venv\Scripts\python.exe" (
    echo Run setup.bat first.
    pause
    exit /b 1
)
cd backend
if not exist ".env" copy .env.example .env

set PLAYWRIGHT_BROWSERS_PATH=%~dp0backend\playwright-browsers
set BOL_LOGIN_MODE=1

call venv\Scripts\activate

echo.
echo [1/2] Checking Playwright Chromium...
python scripts\ensure_playwright_chromium.py
if errorlevel 1 (
    echo Chromium install failed. Check internet connection.
    pause
    exit /b 1
)

echo.
echo [2/2] Opening Bol login / checking saved session...
echo If a valid session is already in Neon, it will reuse it (no re-login).
echo To force a new login: python scripts\bol_login_sync.py --force
python scripts\bol_login_sync.py
pause
