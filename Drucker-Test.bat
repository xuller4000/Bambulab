@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo .venv fehlt. Erst in PowerShell ausfuehren: py -m venv .venv  und  .venv\Scripts\pip install -e .
  pause
  exit /b 1
)
".venv\Scripts\python.exe" check_printer.py
