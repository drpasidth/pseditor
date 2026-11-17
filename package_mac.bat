@echo off
setlocal
cd /d "%~dp0"
title Package PSEditor for macOS

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

"%PY%" package_mac.py
exit /b %ERRORLEVEL%
