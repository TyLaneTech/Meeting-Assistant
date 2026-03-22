@echo off
setlocal
set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"

:: ── Locate Python 3.12 ───────────────────────────────────────────────────────
set "PY312="

:: 1. Try the Windows Python Launcher (py -3.12) — resolves to the actual exe path
py -3.12 --version >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%P in ('py -3.12 -c "import sys; print(sys.executable)"') do set "PY312=%%P"
)
if defined PY312 goto :have_python

:: 2. Check well-known install locations
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "C:\Program Files\Python312\python.exe"
    "C:\Python312\python.exe"
) do if exist %%P if not defined PY312 set "PY312=%%~P"
if defined PY312 goto :have_python

:: 3. Not found — install via winget
echo.
echo  Python 3.12 not found. Installing via winget...
echo.
winget install --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo.
    echo  winget install failed. Please install Python 3.12 manually:
    echo  https://www.python.org/downloads/
    echo.
    pause & exit /b 1
)

:: Re-check known paths after install (PATH not refreshed in current session)
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "C:\Program Files\Python312\python.exe"
    "C:\Python312\python.exe"
) do if exist %%P if not defined PY312 set "PY312=%%~P"

if not defined PY312 (
    echo.
    echo  Python 3.12 was installed but could not be located automatically.
    echo  Please close this window and run launch.bat again.
    echo.
    pause & exit /b 1
)

:have_python

:: ── Create venv if needed ────────────────────────────────────────────────────
if not exist "%VENV%\Scripts\python.exe" (
    echo  Creating Python 3.12 environment...
    "%PY312%" -m venv "%VENV%"
    if errorlevel 1 (
        echo  Failed to create virtual environment.
        pause & exit /b 1
    )
)

"%VENV%\Scripts\python.exe" "%ROOT%launch.py"
if errorlevel 1 pause
