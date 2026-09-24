@echo off
REM Starts the dashboard and opens it in your browser.
cd /d "%~dp0"
set PYTHONUTF8=1
start "" http://127.0.0.1:8766
.venv\Scripts\python dashboard.py --port 8766
pause
