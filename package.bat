@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Package PSEditor installer

if not exist "install.bat" (
  echo install.bat is missing. Run this from the PSEditor folder.
  exit /b 1
)

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

rem Bundle wheels so install.bat works on a PC without internet.
echo Downloading Python packages into wheels\ ...
if exist "wheels" rmdir /s /q "wheels"
mkdir "wheels"
set "GOT=0"
for %%V in (3.10 3.11 3.12 3.13 3.14) do (
  "%PY%" -m pip download -r requirements.txt -d wheels --only-binary=:all: --platform win_amd64 --implementation cp --python-version %%V -q
  if errorlevel 1 (
    echo   Python %%V: some packages have no wheel, skipped.
  ) else (
    echo   Python %%V: OK
    set "GOT=1"
  )
)
if "!GOT!"=="0" (
  echo Could not download packages. This PC needs internet to build the offline bundle.
  exit /b 1
)

if not exist "dist" mkdir "dist"
if exist "dist\PSEditor-install.zip" del /f "dist\PSEditor-install.zip"

rem Zip the app without the local venv, bytecode, or this PC's database
rem (db.sqlite3 stores agent API keys in plain text).
tar -a -cf "dist\PSEditor-install.zip" --exclude=.venv --exclude=__pycache__ --exclude=*.pyc --exclude=db.sqlite3 --exclude=db.sqlite3-journal --exclude=install.log --exclude=wheels_mac --exclude=dist .
if errorlevel 1 (
  echo Failed to create the zip. Windows 10 or newer includes tar.exe.
  exit /b 1
)

echo.
echo Created:
echo   %CD%\dist\PSEditor-install.zip
echo.
echo On the other PC: unzip, then double-click install.bat (no internet needed for the Python packages).
echo.
exit /b 0
