@echo off
chcp 65001 >nul 2>&1
title Self Test
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

echo ---- 1/5 fault injection loop ----
"%PY%" "%~dp0tools\test_faults.py"
if errorlevel 1 goto failed

echo.
echo ---- 2/5 acceptance reverse test ----
"%PY%" "%~dp0tools\test_accept.py"
if errorlevel 1 goto failed

echo.
echo ---- 3/5 generation loop with guardrails ----
"%PY%" "%~dp0tools\test_nl.py"
if errorlevel 1 goto failed

echo.
echo ---- 4/5 command-level guardrails ----
"%PY%" "%~dp0tools\test_cmdguard.py"
if errorlevel 1 goto failed

echo.
echo ---- 5/5 RIP v1/v2 and OSPF wildcard config ----
"%PY%" "%~dp0tools\test_cfgprofile.py"
if errorlevel 1 goto failed

echo.
echo ALL PASS
goto end

:failed
echo.
echo FAILED - see output above

:end
pause
