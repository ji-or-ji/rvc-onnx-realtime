@echo off
rem ASCII only: cmd reads .bat as ANSI. Chinese output comes from Python.
chcp 65001 >nul
title RVC Web Console
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
echo Starting web console ... browser will open http://127.0.0.1:8899
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8899"
venv-dml\Scripts\python.exe web_ui.py
pause
