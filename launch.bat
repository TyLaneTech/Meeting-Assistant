@echo off
setlocal
set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo  Python is not installed or not in your PATH.
    echo  Download from: https://www.python.org/downloads/
    echo  Check "Add python.exe to PATH" during install.
    echo.
    pause & exit /b 1
)

if not exist "%VENV%\Scripts\python.exe" (
    echo  Creating Python environment...
    python -m venv "%VENV%"
    if errorlevel 1 (
        echo  Failed to create virtual environment.
        pause & exit /b 1
    )
)

"%VENV%\Scripts\python.exe" "%ROOT%launch.py"
if errorlevel 1 pause
