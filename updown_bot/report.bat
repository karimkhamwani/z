@echo off
REM Double-click shortcut for Windows. Same as:  py -3 manage.py report %*
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 manage.py report %*) else (python manage.py report %*)
pause
