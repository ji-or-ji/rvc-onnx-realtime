@echo off
rem ASCII only (cmd reads .bat as ANSI).
chcp 65001 >nul
echo Opening control panel: http://127.0.0.1:8898
start "" http://127.0.0.1:8898
