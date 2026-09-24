@echo off
REM One-time setup: creates .venv and installs dependencies. Needs Python 3.11+ from python.org.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)
%PY% -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'" || (echo Install Python 3.11 or newer from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b 1)
%PY% -m venv .venv || (echo Could not create the virtual environment. & pause & exit /b 1)
.venv\Scripts\python -m pip install --upgrade pip >nul
.venv\Scripts\python -m pip install -r requirements.txt || (echo pip install failed - see above. & pause & exit /b 1)
.venv\Scripts\python -m unittest tests.test_core
echo.
echo Setup done. Next: double-click start_bot.bat, then start_dashboard.bat
pause
