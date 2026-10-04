@echo off
rem ASCII only in this file: cmd reads .bat as ANSI, so non-ASCII here becomes mojibake.
rem All Chinese messages are printed by Python instead.
chcp 65001 >nul
title RVC Realtime - Bootstrap
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
  echo.
  echo [ERROR] Python 3 not found.
  echo         Install Python 3.10+ and check "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

%PY% bootstrap.py %*

echo.
pause
