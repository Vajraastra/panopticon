@echo off
title Panopticon Launcher
setlocal enabledelayedexpansion
color 0A

echo.
echo ===================================================
echo     PANOPTICON RELOADED - Modular Image Organizer
echo ===================================================
echo.

set VENV_DIR=venv

:: 1. Check/Install Python 3.12
py -3.12 --version >nul 2>&1
if !errorlevel! neq 0 (
    echo [SETUP] Python 3.12 not found. Installing via Winget...
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements
    if !errorlevel! neq 0 (
        color 0C
        echo [ERROR] Failed to install Python. Please install manually.
        pause
        exit /b
    )
    timeout /t 5
)

:: 2. Setup Venv
if not exist %VENV_DIR% (
    echo [SETUP] Creating new virtual environment...
    py -3.12 -m venv %VENV_DIR%
)

:: 3. Activate
call "%VENV_DIR%\Scripts\activate.bat"

:: 4. Dependencies
echo [SETUP] Checking dependencies...
echo [SETUP] Installing dependeces (Verbose Mode)...
python -m pip install -r requirements.txt
if !errorlevel! neq 0 (
    color 0C
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b
)

:: 5. Launch
echo [LAUNCH] Starting application...
python main.py

if !errorlevel! neq 0 (
    color 0C
    echo.
    echo [CRASH] The application crashed.
    pause
) else (
    echo [EXIT] Application closed.
)
