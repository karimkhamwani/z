@echo off
REM Double-click shortcut for Windows. Same as:  py -3 manage.py run %*
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 manage.py run %*) else (python manage.py run %*)
pause
