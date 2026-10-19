# start_sovits.ps1 - Start GPT-SoVITS API server
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:HF_ENDPOINT = "https://hf-mirror.com"

$sovitsDir = "F:\GPT-SoVITS-main"
$python = "F:\envs\gpt_sovits\Scripts\python.exe"
$libBin = "F:\envs\gpt_sovits\Library\bin"

# Ensure PATH includes env Library\bin (DLL dependencies)
$env:PATH = "$libBin;$env:PATH"

# numba cache: redirect JIT cache to a writable dir to avoid PermissionError stall
$env:NUMBA_CACHE_DIR = "$env:LOCALAPPDATA\Temp\numba_cache_sovits"

Write-Host "Starting GPT-SoVITS API server..." -ForegroundColor Cyan
Write-Host "  Dir: $sovitsDir"
Write-Host "  Python: $python"
Write-Host "  Port: 9880"
Write-Host ""

# Check if port is already in use
$existing = Get-NetTCPConnection -LocalPort 9880 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[!] Port 9880 already in use (PID $($existing.OwningProcess)), GPT-SoVITS may already be running" -ForegroundColor Yellow
    Write-Host "    Verify at http://127.0.0.1:9880/"
    exit 0
}

Set-Location $sovitsDir
& $python api_v2.py -a 127.0.0.1 -p 9880
