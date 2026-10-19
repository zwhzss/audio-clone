@echo off
chcp 65001 >nul
title Frontend Dev Server (auto proxy to backend)
cd /d %~dp0frontend
echo Starting frontend dev server...
echo   - App:      http://localhost:5173
echo   - Backend:  http://127.0.0.1:8000
echo   - SoVITS:   http://127.0.0.1:9880
echo.
npx vite
