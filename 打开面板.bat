@echo off
rem ASCII only (cmd reads .bat as ANSI). Opens the console panel on 8899.
chcp 65001 >nul
setlocal
set "HOST=127.0.0.1"
set "PORT=8899"

powershell -NoProfile -Command "$c=New-Object Net.Sockets.TcpClient; try{$c.Connect('%HOST%',%PORT%); $c.Close(); exit 0}catch{exit 1}" >nul 2>nul
if errorlevel 1 (
  echo.
  echo Console is not running on port %PORT%.
  echo Start it first:  run  启动.bat   or   python console.py
  echo.
  pause
  exit /b 1
)

set "TK="
if exist "%~dp0control_token.txt" set /p TK=<"%~dp0control_token.txt"

set "URL=http://%HOST%:%PORT%/"
if not "%TK%"=="" set "URL=http://%HOST%:%PORT%/?k=%TK%"
start "" "%URL%"
