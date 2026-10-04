@echo off
chcp 65001 >nul
title RVC Web Console
cd /d "%~dp0"
echo 启动 Web 控制台 ...  浏览器会自动打开 http://127.0.0.1:8899
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8899"
venv-dml\Scripts\python.exe web_ui.py
pause
