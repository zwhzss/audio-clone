@echo off
chcp 65001 >nul
title Audio Clone Studio
cd /d %~dp0

if not exist backend\.env copy backend\.env.example backend\.env >nul

echo [1/2] Starting backend (http://127.0.0.1:8000)...
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
cd backend
if exist ".venv\Scripts\python.exe" (set PY=.venv\Scripts\python.exe) else (set PY=python)
start "" /min "%PY%" run.py
cd ..

timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8000
echo Backend starting, browser will open http://127.0.0.1:8000
echo To stop: close the Backend window.
pause
