@echo off
chcp 65001 >nul 2>&1
title One-Click Topology Generator
setlocal
cd /d "%~dp0"

set "PY=C:\Users\yelin\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" (
  echo.
  echo [ERROR] Python environment not found:
  echo         %PY%
  echo.
  echo Rebuild it with:
  echo   C:\Users\yelin\.workbuddy\binaries\python\versions\3.13.12\python.exe -m venv "%PY%\.."
  echo.
  pause
  exit /b 1
)

set PYTHONIOENCODING=utf-8
"%PY%" "%~dp0tools\wizard.py" %*

echo.
pause
