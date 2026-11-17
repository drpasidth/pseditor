@echo off
setlocal
cd /d "%~dp0"
title PSEditor Installer
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
exit /b %ERRORLEVEL%
