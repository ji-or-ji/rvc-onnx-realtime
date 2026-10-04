@echo off
chcp 65001 >nul
title RVC Realtime (ONNX)
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
echo ============  RVC realtime (ONNX backend)  ============
echo   [1] CPU              - monitor speakers (no GPU)
echo   [2] RTX 2060 (DML)   - monitor speakers, fastest
echo   [3] Intel iGPU       - not recommended
echo   [4] RTX 2060 (DML)   -^> CABLE Input  (for OBS capture)
echo   [5] CPU              -^> CABLE Input  (for OBS capture)
echo ======================================================
set /p sel=Choose 1-5:
if "%sel%"=="1" goto a1
if "%sel%"=="2" goto a2
if "%sel%"=="3" goto a3
if "%sel%"=="4" goto a4
if "%sel%"=="5" goto a5
echo Invalid choice.
pause
exit /b 1

:a1
venv-dml\Scripts\python.exe onnx_rt.py --ep cpu  --block 0.5 --ctx 0.25 --f0 pm
goto end
:a2
venv-dml\Scripts\python.exe onnx_rt.py --ep dml0 --block 0.5 --ctx 0.25 --f0 pm
goto end
:a3
venv-dml\Scripts\python.exe onnx_rt.py --ep dml1 --block 0.5 --ctx 0.25 --f0 pm
goto end
:a4
venv-dml\Scripts\python.exe onnx_rt.py --ep dml0 --block 0.5 --ctx 0.25 --f0 pm --out "CABLE Input"
goto end
:a5
venv-dml\Scripts\python.exe onnx_rt.py --ep cpu  --block 0.5 --ctx 0.25 --f0 pm --out "CABLE Input"
goto end
:end
pause
