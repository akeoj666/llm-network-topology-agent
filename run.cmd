@echo off
setlocal
set "PY=C:\Users\yelin\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if "%~1"=="" goto usage
if exist "%~dp0examples\%~1.yaml" (
  "%PY%" "%~dp0gen.py" "%~dp0examples\%~1.yaml" -o "%~dp0out"
  goto end
)
"%PY%" "%~dp0gen.py" "%~1" -o "%~dp0out"
goto end
:usage
echo usage: run ^<uc01^|uc02^|uc03^|uc05^|path\to\dsl.yaml^>
exit /b 1
:end
