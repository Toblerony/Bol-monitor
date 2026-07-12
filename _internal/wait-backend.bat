@echo off
setlocal
set PORT=8003
set /a TRIES=0
set /a MAX=60

:loop
set /a TRIES+=1
curl -sf "http://127.0.0.1:%PORT%/health" >nul 2>&1
if %errorlevel%==0 (
    echo Backend ready on port %PORT%
    exit /b 0
)
if %TRIES% geq %MAX% (
    echo Backend did not respond on port %PORT% after ~2 minutes
    exit /b 1
)
ping -n 3 127.0.0.1 >nul
goto loop
