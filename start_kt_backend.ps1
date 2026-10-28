# PowerShell Launcher for Knowledge Tracer Python Backend
$PSScriptRoot = Split-Path -Parent -Path $MyInvocation.MyCommand.Definition
Set-Location -Path "$PSScriptRoot\KT_python\KT_python"

Write-Host "===================================================" -ForegroundColor Green
Write-Host "  Starting Knowledge Tracer Python Backend (:8081)" -ForegroundColor Green
Write-Host "  Dashboard: http://127.0.0.1:8081/dashboard.html" -ForegroundColor Cyan
Write-Host "===================================================" -ForegroundColor Green
Write-Host ""

$conn = Get-NetTCPConnection -LocalPort 8081 -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    Write-Host "Freeing port 8081 (PID: $($conn.OwningProcess))..." -ForegroundColor Yellow
    Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

if (Test-Path ".venv\Scripts\python.exe") {
    Write-Host "Using Python virtual environment (.venv)..." -ForegroundColor Cyan
    & .venv\Scripts\python.exe scripts\run_server.py
} else {
    Write-Host "Using system Python..." -ForegroundColor Cyan
    python scripts\run_server.py
}
