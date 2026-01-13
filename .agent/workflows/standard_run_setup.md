---
description: Standardized run.bat script for Python environment setup and launch
---

# Standardized Run Script

This workflow provides a robust `run.bat` script that handles:
1.  **Virtual Environment Creation**: Automatically checks for `venv` and creates it if missing.
2.  **Activation**: Activates the environment silently.
3.  **Pip Upgrade**: Upgrades `pip` to the latest version to avoid dependency issues.
4.  **Dependency Verification**: Installs dependencies from `requirements.txt`.
5.  **Error Handling**: Pauses and changes color on error to allow reading logs.

## Script Content (`run.bat`)

```batch
@echo off
title Python Project Launcher
color 0A

echo ---------------------------------------------------
echo     PYTHON PROJECT LAUNCHER - Standardized
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
if exist requirements.txt (
    python -m pip install -r requirements.txt --prefer-binary
    if %errorlevel% neq 0 (
        color 0C
        echo [ERROR] Failed to install dependencies.
        echo Please check your internet connection or Python installation.
        pause
        exit /b
    )
) else (
    echo [WARNING] requirements.txt not found! Skipping dependency check.
)

:: 5. Launch App
echo.
echo [LAUNCH] Starting Application...
:: REPLACE 'main.py' WITH YOUR ENTRY POINT
python main.py

if %errorlevel% neq 0 (
    color 0C
    echo [CRASH] Application closed with an error.
    pause
) else (
    echo [EXIT] Application closed normally.
    timeout /t 3 >nul
)
```

## Usage
1.  Copy this content into a file named `run.bat` in your project root.
2.  Ensure you have a `requirements.txt` and a `main.py` (or update the script to point to your entry file).
3.  Double-click `run.bat` to start your project.
