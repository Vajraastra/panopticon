@echo off
title Panopticon Launcher
color 0A

echo ---------------------------------------------------
echo     PANOPTICON - Modular Image Organizer
echo ---------------------------------------------------

set VENV_DIR=venv

:: 1. Virtual Environment Check
if not exist %VENV_DIR% (
    echo [SETUP] Creating virtual environment...
    python -m venv %VENV_DIR%
)

:: 2. Activate Venv
echo [SETUP] Activating environment...
call %VENV_DIR%\Scripts\activate

:: 3. Upgrade pip (Essential for modern wheels)
echo [SETUP] Updating package manager...
python -m pip install --upgrade pip

:: 4. Install/Verify Dependencies
echo [SETUP] Verifying dependencies (This may take a moment)...
python -m pip install -r requirements.txt --prefer-binary
if %errorlevel% neq 0 (
    color 0C
    echo [ERROR] Failed to install dependencies.
    echo Please check your internet connection or Python installation.
    pause
    exit /b
)

:: 5. Launch App
echo.
echo [LAUNCH] Starting Panopticon...
python main.py

if %errorlevel% neq 0 (
    color 0C
    echo [CRASH] Application closed with an error.
    pause
) else (
    echo [EXIT] Application closed normally.
    timeout /t 3 >nul
)
