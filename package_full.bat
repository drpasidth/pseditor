@echo off
setlocal
cd /d "%~dp0"
title Package PSEditor (full: .venv + db.sqlite3)

if not exist ".venv\Scripts\python.exe" (
  echo .venv is missing. Run install.bat here first.
  pause
  exit /b 1
)
if not exist "db.sqlite3" (
  echo db.sqlite3 is missing. Run run.bat once first.
  pause
  exit /b 1
)
if not exist "wheels\*.whl" (
  echo Note: wheels\ is missing. If the other PC has no C:\Python313, install.bat
  echo will need internet to rebuild .venv. Run package.bat once to add wheels\.
  echo.
)

echo WARNING: db.sqlite3 contains your agent API keys in plain text.
echo Only copy this zip to a PC you trust.
echo.
echo Stop PSEditor (close run.bat) before continuing.
pause

if not exist "dist" mkdir "dist"
if exist "dist\PSEditor-full.zip" del /f "dist\PSEditor-full.zip"

echo Creating zip...
tar -a -cf "dist\PSEditor-full.zip" --exclude=__pycache__ --exclude=*.pyc --exclude=install.log --exclude=wheels_mac --exclude=dist .
if errorlevel 1 (
  echo Failed to create the zip. Windows 10 or newer includes tar.exe.
  pause
  exit /b 1
)

echo.
echo Created:
echo   %CD%\dist\PSEditor-full.zip
echo.
echo On the other PC: unzip, then double-click install.bat.
echo .venv works as-is if that PC has Python 3.13 at C:\Python313;
echo otherwise install.bat rebuilds it from wheels\. Your data in db.sqlite3 is kept.
echo.
pause
exit /b 0
