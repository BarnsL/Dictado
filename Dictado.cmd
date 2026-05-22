@echo off
REM =====================================================================
REM   Dictado - one-click launcher (Windows)
REM =====================================================================
REM
REM   Double-click this file or pin a shortcut to it on your taskbar to
REM   start Dictado. The daemon lives in the system tray; close it from
REM   the tray icon's right-click menu.
REM
REM   What it does, top to bottom:
REM     1. Locate a system Python interpreter (3.10 or newer).
REM     2. Add the local venv's site-packages to PYTHONPATH if one
REM        exists alongside this script, so dependencies resolve
REM        without needing to "activate" the venv first.
REM     3. Spawn `pythonw -m dictado` so no console window is left
REM        behind.
REM
REM   Why a .cmd instead of a .exe?
REM     Bundling Whisper + PyTorch + ffmpeg with PyInstaller produces
REM     a 2 GB+ blob. A .cmd keeps the install footprint tiny and
REM     plays nicely with whatever Python you already have. Pin a
REM     shortcut to this file to your taskbar and it behaves the same
REM     as a one-click .exe would.
REM
REM   First-run reminders:
REM     - The first launch downloads Whisper weights (~1.5 GB for the
REM       default `medium` model). Allow about a minute on a typical
REM       broadband connection.
REM     - Python 3.10 or newer is required. Install from
REM       https://www.python.org/downloads/ if you don't have it yet.
REM =====================================================================

setlocal

REM --- Locate the Dictado install root (this script's directory) ---
set "DICTADO_ROOT=%~dp0"
if "%DICTADO_ROOT:~-1%"=="\" set "DICTADO_ROOT=%DICTADO_ROOT:~0,-1%"

REM --- Find a system Python interpreter ---
REM Prefer the most recent Python under Program Files (PSF-signed).
REM Running from a signed location side-steps endpoint-protection
REM heuristics that flag pythonw running out of %LOCALAPPDATA%\*\venv\.
set "PYTHONW="
for %%v in (313 312 311 310) do (
    if exist "%ProgramFiles%\Python%%v\pythonw.exe" (
        if not defined PYTHONW set "PYTHONW=%ProgramFiles%\Python%%v\pythonw.exe"
    )
)
if not defined PYTHONW (
    REM Fall back to whatever's on PATH.
    for /f "delims=" %%i in ('where pythonw 2^>nul') do (
        if not defined PYTHONW set "PYTHONW=%%i"
    )
)
if not defined PYTHONW (
    echo [Dictado] No Python 3.10+ pythonw.exe found.
    echo           Install Python from https://www.python.org/downloads/
    pause
    exit /b 1
)

REM --- Prefer a sibling .venv's site-packages on PYTHONPATH if present ---
set "VENV_SITE=%DICTADO_ROOT%\.venv\Lib\site-packages"
if exist "%VENV_SITE%" (
    set "PYTHONPATH=%VENV_SITE%;%DICTADO_ROOT%;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%DICTADO_ROOT%;%PYTHONPATH%"
)

REM --- Run as a module so we don't depend on the console_scripts shim
REM     being on PATH after `pip install --user .`.
start "" "%PYTHONW%" -m dictado

endlocal
