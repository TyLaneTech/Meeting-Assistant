@echo off
setlocal
set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"

:: ── Locate Python 3.10, 3.11, or 3.12 ───────────────────────────────────────
set "PYEXE="

:: 1. Try the Windows Python Launcher for each acceptable version (prefer newest)
for %%V in (3.12 3.11 3.10) do (
    if not defined PYEXE (
        py -%%V --version >nul 2>&1
        if not errorlevel 1 (
            for /f "delims=" %%P in ('py -%%V -c "import sys; print(sys.executable)"') do set "PYEXE=%%P"
        )
    )
)
if defined PYEXE goto :have_python

:: 2. Check well-known install locations (newest first)
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "C:\Program Files\Python312\python.exe"
    "C:\Program Files\Python311\python.exe"
    "C:\Program Files\Python310\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "C:\Python310\python.exe"
) do if exist %%P if not defined PYEXE set "PYEXE=%%~P"
if defined PYEXE goto :have_python

:: 3. Not found — install Python 3.12 via winget
echo.
echo  Python 3.10, 3.11, or 3.12 not found. Installing Python 3.12 via winget...
echo.
winget install --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo.
    echo  winget install failed. Please install Python 3.10+ manually:
    echo  https://www.python.org/downloads/
    echo.
    pause & exit /b 1
)

:: Re-check known paths after install (PATH not refreshed in current session)
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "C:\Program Files\Python312\python.exe"
    "C:\Python312\python.exe"
) do if exist %%P if not defined PYEXE set "PYEXE=%%~P"

if not defined PYEXE (
    echo.
    echo  Python 3.12 was installed but could not be located automatically.
    echo  Please close this window and run launch.bat again.
    echo.
    pause & exit /b 1
)

:have_python

:: ── Create venv if needed ────────────────────────────────────────────────────
if not exist "%VENV%\Scripts\python.exe" (
    echo  Creating Python environment...
    "%PYEXE%" -m venv "%VENV%"
    if errorlevel 1 (
        echo  Failed to create virtual environment.
        pause & exit /b 1
    )
)

"%VENV%\Scripts\python.exe" "%ROOT%launch.py"
if errorlevel 1 pause
