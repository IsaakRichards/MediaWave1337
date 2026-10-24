@echo off
rem MediaWave1337 setup for Windows: finds Python 3, then opens the setup wizard.
cd /d "%~dp0"

set "PY="
set "PYW="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul && set "PY=py -3" && set "PYW=pyw -3"
if not defined PY (
    where python >nul 2>nul && python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul && set "PY=python" && set "PYW=pythonw"
)

if not defined PY (
    echo MediaWave1337 needs Python 3.9 or newer, which was not found.
    echo.
    echo Opening the Python download page. Install Python, tick "Add python.exe to PATH",
    echo then run setup.bat again.
    start "" "https://www.python.org/downloads/windows/"
    pause
    exit /b 1
)

%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo The setup wizard needs tkinter. Re-run the Python installer, choose Modify,
    echo and make sure "tcl/tk and IDLE" is selected.
    pause
    exit /b 1
)

start "" %PYW% setup_wizard.py
