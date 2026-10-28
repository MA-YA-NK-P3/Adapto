@echo off
title Knowledge Tracer Python Backend (Port 8081)
cd /d "%~dp0KT_python\KT_python"

echo ===================================================
echo   Starting Knowledge Tracer Python Backend (:8081)
echo   Dashboard: http://127.0.0.1:8081/dashboard.html
echo ===================================================
echo.

rem Free port 8081 if previously occupied by a stale process
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8081" ^| findstr "LISTENING"') do (
    echo Freeing port 8081 PID %%a...
    taskkill /F /PID %%a >nul 2>&1
)

if exist ".venv\Scripts\python.exe" (
    echo Using Python virtual environment .venv
    .venv\Scripts\python.exe scripts\run_server.py
) else (
    echo Using system Python
    python scripts\run_server.py
)

pause
