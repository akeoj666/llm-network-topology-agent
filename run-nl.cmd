@echo off
setlocal
set "PY=C:\Users\yelin\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if "%~1"=="" goto usage
"%PY%" "%~dp0nlgen.py" %*
goto end
:usage
echo usage: run-nl "three routers two PCs with RIP"
exit /b 1
:end
