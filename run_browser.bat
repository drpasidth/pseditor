@echo off
rem Old behavior: run the server and open PSEditor in your default web browser.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
  set "PY=venv\Scripts\python.exe"
) else (
  set "PY=python"
)

echo Using: %PY%
"%PY%" manage.py migrate --noinput
if errorlevel 1 (
  echo Migration failed.
  pause
  exit /b 1
)

rem Open browser after a short delay so the first page load hits a live server
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:8800/"

echo.
echo PSEditor running at http://127.0.0.1:8800/
echo Press Ctrl+C to stop.
echo.
"%PY%" manage.py runserver 127.0.0.1:8800
pause
