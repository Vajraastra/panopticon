@echo off
title Panopticon Launcher
setlocal enabledelayedexpansion
color 0A

echo ---------------------------------------------------
echo     PANOPTICON - Modular Image Organizer
echo ---------------------------------------------------

set VENV_DIR=venv

:: 1. Verify/Install Python 3.12
echo [SETUP] Checking for Python 3.12...
py -3.12 --version >nul 2>&1
if !errorlevel! equ 0 goto :python_ok

echo [WARNING] Python 3.12 NOT found. 
echo [SETUP] Installing Python 3.12 via Winget...
winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
if !errorlevel! neq 0 (
    color 0C
    echo [ERROR] Automated installation failed.
    echo Please install Python 3.12 manually from python.org
    pause
    exit /b
)
timeout /t 5 >nul

:python_ok

:: 2. Virtual Environment Check & Create
if not exist %VENV_DIR% goto :create_venv

echo [SETUP] Verifying existing environment version...
"%VENV_DIR%\Scripts\python.exe" --version 2>&1 | findstr "3.12" >nul
if !errorlevel! equ 0 goto :activate_venv

echo [WARNING] Existing venv is not Python 3.12. 
echo [SETUP] Recreating environment to avoid AI collisions...
rmdir /s /q %VENV_DIR%

:create_venv
echo [SETUP] Creating virtual environment (Python 3.12)...
py -3.12 -m venv %VENV_DIR%
if !errorlevel! neq 0 (
    color 0C
    echo [ERROR] Failed to create venv with Python 3.12.
    pause
    exit /b
)

:activate_venv
echo [SETUP] Activating environment...
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    color 0C
    echo [ERROR] Activation script not found. 
    pause
    exit /b
)
call "%VENV_DIR%\Scripts\activate.bat"

:: 5. Upgrade pip
echo [SETUP] Updating package manager...
python -m pip install --upgrade pip

:: 6. Install Dependencies
echo [SETUP] Verifying dependencies...
python -m pip install -r requirements.txt --prefer-binary
if !errorlevel! neq 0 (
    color 0C
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b
)

:: 7. Launch App
echo.
echo [LAUNCH] Starting Panopticon...
python main.py

if !errorlevel! neq 0 (
    color 0C
    echo [CRASH] Application closed with an error (Code: !errorlevel!).
    pause
) else (
    echo [EXIT] Application closed normally.
    timeout /t 3 >nul
)
