@echo off
chcp 65001 >nul
title RVC Realtime - 一键启动
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
  echo.
  echo 没找到 Python 3。请先装 Python 3.10+ 并勾选 "Add python.exe to PATH"。
  echo.
  pause
  exit /b 1
)

%PY% bootstrap.py %*

echo.
pause
