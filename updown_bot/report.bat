@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
.venv\Scripts\python report.py %*
pause
