@echo off
chcp 65001 >nul
title Audio Clone Studio (with GPT-SoVITS)
cd /d %~dp0

if not exist backend\.env copy backend\.env.example backend\.env >nul

echo [1/3] Starting GPT-SoVITS API (http://127.0.0.1:9880)...
start "" /min powershell -ExecutionPolicy Bypass -File "%~dp0start_sovits.ps1"

echo [2/3] Starting backend (http://127.0.0.1:8000)...
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
cd backend
if exist ".venv\Scripts\python.exe" (set PY=.venv\Scripts\python.exe) else (set PY=python)
start "" /min "%PY%" run.py
cd ..

timeout /t 5 /nobreak >nul
start "" http://127.0.0.1:8000
echo Browser will open http://127.0.0.1:8000
echo Wait for GPT-SoVITS model loading before using voice clone.
pause
