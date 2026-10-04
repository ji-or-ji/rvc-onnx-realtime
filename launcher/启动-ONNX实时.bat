@echo off
chcp 65001 >nul
title RVC Realtime (ONNX)
cd /d "%~dp0"
echo ============  RVC realtime (ONNX backend)  ============
echo   [1] CPU            - no GPU used (slowest but safe)
echo   [2] RTX 2060 (DML) - fastest, small GPU load
echo   [3] Intel iGPU     - not recommended (too slow)
echo ======================================================
set /p sel=Choose 1/2/3:
if "%sel%"=="1" set EP=cpu
if "%sel%"=="2" set EP=dml0
if "%sel%"=="3" set EP=dml1
if not defined EP (echo Invalid choice. & pause & exit /b)
echo Launching with --ep %EP% ...
venv-dml\Scripts\python.exe onnx_rt.py --ep %EP% --block 0.5 --ctx 0.25 --f0 pm
pause
