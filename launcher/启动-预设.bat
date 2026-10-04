@echo off
chcp 65001 >nul
title RVC Realtime - Preset Launcher
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0presets\run_preset.ps1"
echo.
pause
