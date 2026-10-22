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

echo ---- 1/8 fault injection loop ----
"%PY%" "%~dp0tools\test_faults.py"
if errorlevel 1 goto failed

echo.
echo ---- 2/8 acceptance reverse test ----
"%PY%" "%~dp0tools\test_accept.py"
if errorlevel 1 goto failed

echo.
echo ---- 3/8 generation loop with guardrails ----
"%PY%" "%~dp0tools\test_nl.py"
if errorlevel 1 goto failed

echo.
echo ---- 4/8 command-level guardrails ----
"%PY%" "%~dp0tools\test_cmdguard.py"
if errorlevel 1 goto failed

echo.
echo ---- 5/8 RIP v1/v2 and OSPF wildcard config ----
"%PY%" "%~dp0tools\test_cfgprofile.py"
if errorlevel 1 goto failed

echo.
echo ---- 6/8 config-level diagnosis and repair (C3) ----
"%PY%" "%~dp0tools\test_diag.py"
if errorlevel 1 goto failed

echo.
echo ---- 7/8 IP/VLAN addressing plan ----
"%PY%" "%~dp0tools\test_plan.py"
if errorlevel 1 goto failed

echo.
echo ---- 8/8 pkt consistency check ----
"%PY%" "%~dp0tools\test_pkt.py"
if errorlevel 1 goto failed

echo.
echo ALL PASS
goto end

:failed
echo.
echo FAILED - see output above

:end
pause
