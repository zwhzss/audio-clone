@echo off
chcp 65001 >nul
title Audio Clone Studio
cd /d %~dp0

echo ========================================
echo   Audio Clone Studio - Quick Start
echo ========================================
echo.

:: 1. Build frontend if dist missing
if exist "frontend\dist\index.html" goto :skip_build
echo [1/3] Building frontend...
cd frontend
if not exist node_modules call npm install
call npm run build
if errorlevel 1 goto :build_fail
cd ..
:skip_build
echo [1/3] Frontend ready.

:: 2. Start GPT-SoVITS if not running
echo [2/3] Checking GPT-SoVITS...
netstat -ano | findstr ":9880" | findstr "LISTENING" >nul
if not errorlevel 1 goto :sovits_ok
if not exist "F:\GPT-SoVITS-main\api_v2.py" goto :sovits_skip
start "" /min powershell -ExecutionPolicy Bypass -File "%~dp0start_sovits.ps1"
echo       GPT-SoVITS starting...
goto :sovits_ok
:sovits_skip
echo       GPT-SoVITS not found, skipping.
:sovits_ok

:: 3. Start backend if not running
echo [3/3] Checking backend...
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if not errorlevel 1 goto :backend_ok
set wait_count=0
if not exist backend\.env copy backend\.env.example backend\.env >nul
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
cd backend
if exist ".venv\Scripts\python.exe" (set PY=.venv\Scripts\python.exe) else (set PY=python)
start "" /min "%PY%" run.py
cd ..
:wait_loop
timeout /t 1 /nobreak >nul
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if not errorlevel 1 goto :backend_ok
set /a wait_count+=1
if %wait_count% geq 30 goto :backend_timeout
goto :wait_loop
:backend_timeout
echo [ERROR] Backend failed to start after 30 seconds!
pause
exit /b 1
:backend_ok
echo       Backend ready.

echo.
echo ========================================
echo   App:    http://127.0.0.1:8000
echo   SoVITS: http://127.0.0.1:9880
echo ========================================
echo.
start "" http://127.0.0.1:8000
pause
exit /b 0

:build_fail
echo [ERROR] Frontend build failed!
cd ..
pause
exit /b 1
