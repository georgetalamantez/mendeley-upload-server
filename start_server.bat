@echo off
title Mendeley Upload Server
echo ===================================================
echo      Starting Mendeley Upload Server
echo ===================================================

echo.
echo [1/3] Checking Python installation...
python --version
if %errorlevel% neq 0 (
    echo Python is not installed or not in PATH.
    echo Please install Python from https://python.org
    pause
    exit
)

echo.
echo [2/3] Installing/Updating dependencies...
pip install -r requirements.txt

echo.
echo [3/3] Starting Server...
echo The dashboard will open in your browser shortly...
echo Press Ctrl+C in this window to stop the server.
echo.

start http://localhost:8000
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000

pause
