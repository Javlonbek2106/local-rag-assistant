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

REM Database passwords: strong random files in a private folder OUTSIDE the project (docs\postgres-hardening.md).
REM Only the folder name is printed; no password is ever shown, passed as an argument or kept in this file.
set "RAG_DB_HARDENED=1"
set "RAG_SECRETS_DIR="
for /f "usebackq delims=" %%d in (`".venv\Scripts\python.exe" -m rag db-secrets`) do set "RAG_SECRETS_DIR=%%d"
if not defined RAG_SECRETS_DIR (
    echo Parol fayllari tayyorlanmadi ^(yuqoridagi xatoga qarang^).
    pause
    exit /b 1
)

docker compose up -d --wait
if errorlevel 1 (
    echo Qdrant yoki PostgreSQL ishga tushmadi.
    pause
    exit /b 1
)

REM PostgreSQL roles (rag_bootstrap / rag_migrator / rag_app). The first run, and a run after an old database, sets them
REM up; every later run only checks. An old database keeps all its data.
REM The key that encrypts the passwords of external databases (only when the connector is on; never replaces a key).
if "%RAG_CONNECTOR_ENABLED%"=="1" ".venv\Scripts\python.exe" -m rag connector key-init >nul
if /i "%RAG_CONNECTOR_ENABLED%"=="true" ".venv\Scripts\python.exe" -m rag connector key-init >nul

".venv\Scripts\python.exe" -m rag db-check >nul 2>&1
if errorlevel 1 (
    echo PostgreSQL rollari sozlanmoqda...
    ".venv\Scripts\python.exe" -m rag db-setup
    if errorlevel 3 (
        echo.
        echo Eski baza topildi. Hech narsa o'zgartirilmadi. Yuqoridagi ogohlantirishni o'qing, zaxira oling
        echo ^(python -m rag backup^) va keyin: python -m rag db-setup --adopt-legacy
        pause
        exit /b 1
    )
    if errorlevel 1 (
        echo PostgreSQL xavfsizlik sozlamasi bajarilmadi. docs\postgres-hardening.md ga qarang.
        pause
        exit /b 1
    )
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
