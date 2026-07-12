@echo off
echo Starting Frontend Dashboard...
cd /d "%~dp0\..\frontend"
if not exist "node_modules" (
    echo Installing npm packages...
    call npm install
)
echo Frontend running at http://localhost:5175
npm run dev
