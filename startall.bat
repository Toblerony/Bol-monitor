@echo off
cd /d "%~dp0"

if not exist "backend\venv\Scripts\python.exe" (
    echo First time: run setup.bat then startall.bat again.
    pause
    exit /b 1
)

echo Stopping any running backend and frontend...
for %%P in (8003 5175) do (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%%P" ^| findstr "LISTENING"') do (
        echo   Port %%P PID %%a
        taskkill /PID %%a /F >nul 2>&1
    )
)

ping -n 3 127.0.0.1 >nul

start "Bol Monitor Backend" cmd /k "%~dp0_internal\boot-backend.bat"
call "%~dp0_internal\wait-backend.bat"
if errorlevel 1 (
    echo Backend still starting — opening frontend anyway. Retry in the app if needed.
) else (
    echo Backend is up.
)
start "Bol Monitor Frontend" cmd /k "%~dp0_internal\boot-frontend.bat"
ping -n 4 127.0.0.1 >nul
start "" "http://localhost:5175"

echo.
echo App: http://localhost:5175
echo Keep both CMD windows open.
echo.
