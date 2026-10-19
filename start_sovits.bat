@echo off
chcp 65001 >nul
title GPT-SoVITS
cd /d F:\GPT-SoVITS-main
set PATH=F:\envs\gpt_sovits\Library\bin;%PATH%
set HF_ENDPOINT=https://hf-mirror.com
set PYTHONUTF8=1
rem numba cache: conda site-packages read-only for current user, redirect to writable temp
set NUMBA_CACHE_DIR=%LOCALAPPDATA%\Temp\numba_cache_sovits
echo Starting GPT-SoVITS API on port 9880...
F:\envs\gpt_sovits\Scripts\python.exe api_v2.py -a 127.0.0.1 -p 9880
