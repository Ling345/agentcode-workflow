@echo off
rem Portable launcher: use whatever Python is on PATH (no hard-coded path).
rem Usage: double-click this file, or run it from a terminal.
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
    python -m agentcode open %*
    goto :end
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m agentcode open %*
    goto :end
)

echo Python not found in PATH. Install Python 3.10 or newer, then run:
echo     python -m pip install -r requirements.txt
echo     python -m agentcode open
pause

:end
