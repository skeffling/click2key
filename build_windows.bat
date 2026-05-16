@echo off
REM Build dist\Whoosh Clicker\Whoosh Clicker.exe. Run from repo root.
REM Assumes a venv at .venv\ (run `py -m venv .venv` once if missing).

setlocal
set PY=.venv\Scripts\python.exe

if not exist "%PY%" (
    echo No venv at .venv\ — run "py -m venv .venv" first, then re-run this script.
    exit /b 1
)

"%PY%" -m pip install -q -e ".[dev]"
if errorlevel 1 exit /b 1

"%PY%" -m PyInstaller --noconfirm clickwhoosh.spec
if errorlevel 1 exit /b 1

echo.
echo Built: dist\Whoosh Clicker\Whoosh Clicker.exe
echo Distribute the entire dist\Whoosh Clicker folder; the .exe needs _internal\ next to it.
