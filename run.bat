@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    python -m venv .venv
    if errorlevel 1 (
        echo Could not create the virtual environment. Is Python installed and on PATH?
        pause
        exit /b 1
    )
)
rem Start the server with no console window; this console closes as soon as it has launched.
rem Use "Shut Down" in the app (or close the app window) to stop it. Errors go to data\server.log.
start "" ".venv\Scripts\pythonw.exe" server.py
