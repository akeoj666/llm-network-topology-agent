@echo off
chcp 65001 >nul 2>&1
title Report D
setlocal
cd /d "%~dp0"

set "PY=C:\Users\yelin\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" (
  echo.
  echo [ERROR] Python environment not found: %PY%
  echo Run start.cmd instead for details.
  echo.
  pause
  exit /b 1
)

set PYTHONIOENCODING=utf-8
"%PY%" "%~dp0tools\report_d.py"

echo.
echo Report: out\D_量化评测.md
pause
