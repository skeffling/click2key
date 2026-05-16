@echo off
REM Build dist\Click2Key\Click2Key.exe. Run from repo root.
REM Assumes a venv at .venv\ (run `py -m venv .venv` once if missing).

setlocal
set PY=.venv\Scripts\python.exe

if not exist "%PY%" (
    echo No venv at .venv\ — run "py -m venv .venv" first, then re-run this script.
    exit /b 1
)

"%PY%" -m pip install -q -e ".[dev]"
if errorlevel 1 exit /b 1

"%PY%" -m PyInstaller --noconfirm click2key.spec
if errorlevel 1 exit /b 1

echo.
echo Built: dist\Click2Key\Click2Key.exe
echo Distribute the entire dist\Click2Key folder; the .exe needs _internal\ next to it.
