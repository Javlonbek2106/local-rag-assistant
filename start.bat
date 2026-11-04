@echo off
setlocal
cd /d "%~dp0"
title Hujjatlar yordamchisi

docker info >nul 2>&1
if errorlevel 1 (
    echo Docker ishlamayapti. Docker Desktop'ni yoqing, ishga tushganini kuting va start.bat ni qayta oching.
    pause
    exit /b 1
)
docker compose up -d
if errorlevel 1 (
    echo Qdrant ishga tushmadi.
    pause
    exit /b 1
)

ollama list >nul 2>&1
if errorlevel 1 (
    echo Ollama yoqilmoqda...
    start "" /b ollama serve
    timeout /t 5 /nobreak >nul
)

REM Models are already downloaded; staying offline keeps startup fast and nothing leaves this computer.
set HF_HUB_OFFLINE=1
set PYTHONIOENCODING=utf-8

start "" cmd /c "timeout /t 8 /nobreak >nul & start http://127.0.0.1:8765"
echo.
echo Ilova: http://127.0.0.1:8765   (to'xtatish uchun shu oynani yoping)
echo.
".venv\Scripts\python.exe" -m rag
pause
