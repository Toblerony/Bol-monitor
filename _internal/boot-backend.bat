@echo off
echo ========================================
echo  Bol Monitor - Backend
echo ========================================
cd /d "%~dp0\.."

cd backend
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
)
call venv\Scripts\activate
if not exist ".env" copy .env.example .env
echo Installing Python dependencies...
pip install -r requirements.txt -q
set PLAYWRIGHT_BROWSERS_PATH=%~dp0..\backend\playwright-browsers
echo.
echo Admin credentials loaded from backend\.env
echo Starting server on port from backend\.env ^(API_PORT^)...
echo.
python run.py
echo.
if errorlevel 1 (
    echo Backend exited with an error.
) else (
    echo Backend stopped.
)
pause
