@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8:replace
pythonw gui.py
if errorlevel 1 python gui.py
