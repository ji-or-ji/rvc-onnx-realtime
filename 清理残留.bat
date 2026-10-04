@echo off
rem ASCII only (cmd reads .bat as ANSI). Chinese output comes from PowerShell.
chcp 65001 >nul
title Clean stray RVC processes
set "PS=powershell -NoProfile -ExecutionPolicy Bypass -Command"

echo.
echo === stray workers of OUR tools (web_ui / onnx_rt / test probes) ===
%PS% "$c = Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'web_ui\.py|onnx_rt\.py|_test_|_probe|bootstrap\.py' }; if (-not $c) { 'none' } else { $c | ForEach-Object { 'kill ' + $_.ProcessId + '  ' + $_.CommandLine.Substring(0,[Math]::Min(70,$_.CommandLine.Length)); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }"

echo.
echo === original realtime_gui (kept running, only listed) ===
%PS% "$c = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'realtime_gui' }; if (-not $c) { 'none' } else { $c | ForEach-Object { 'running ' + $_.ProcessId } }"

echo.
echo === top CPU now ===
%PS% "Get-CimInstance Win32_PerfFormattedData_PerfProc_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne 'Idle' -and $_.Name -ne '_Total' } | Sort-Object PercentProcessorTime -Descending | Select-Object -First 5 Name, IDProcess, PercentProcessorTime | Format-Table -AutoSize"

echo.
pause
