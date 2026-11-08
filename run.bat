@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
    echo Setting up Python environment...
    python -m venv .venv || goto :error
    .venv\Scripts\python.exe -m pip install -r requirements.txt || goto :error
)
start "" .venv\Scripts\pythonw.exe app.py
exit /b 0
:error
echo Setup failed. Make sure Python 3.11+ is installed and on PATH.
pause
