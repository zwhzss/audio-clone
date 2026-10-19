@echo off
chcp 65001 >nul
title Backend Only
cd /d %~dp0backend
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
echo Starting backend on port 8000...
if exist ".venv\Scripts\python.exe" (set PY=.venv\Scripts\python.exe) else (set PY=python)
"%PY%" run.py
