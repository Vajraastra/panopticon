
@echo off
set VENV_DIR=venv

if not exist %VENV_DIR% (
    echo Creating virtual environment...
    python -m venv %VENV_DIR%
)

echo Activating virtual environment...
call %VENV_DIR%\Scripts\activate

echo Checking/Installing requirements...
pip install -r requirements.txt

echo Starting Panopticon...
python main.py

pause
