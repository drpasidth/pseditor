@echo off
rem Starts PSEditor in its own desktop window (no web browser).
rem Use run_browser.bat if you prefer the browser version.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
  set "PYW=.venv\Scripts\pythonw.exe"
) else if exist "venv\Scripts\python.exe" (
  set "PY=venv\Scripts\python.exe"
  set "PYW=venv\Scripts\pythonw.exe"
) else (
  set "PY=python"
  set "PYW=pythonw"
)

echo Using: %PY%
"%PY%" -c "import webview" >nul 2>&1
if errorlevel 1 (
  echo Installing the desktop window component ^(pywebview^)...
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Could not install pywebview. Use run_browser.bat instead, or run install.bat.
    pause
    exit /b 1
  )
)

rem desktop.py migrates the database itself; pythonw keeps this console from staying open.
rem Errors are written to desktop.log.
start "" "%PYW%" desktop.py
exit /b 0
