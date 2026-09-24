@echo off
REM Starts the paper bot. Add --fresh to reset to starting_equity, e.g.:  start_bot.bat --fresh
cd /d "%~dp0"
set PYTHONUTF8=1
.venv\Scripts\python run.py %*
pause
