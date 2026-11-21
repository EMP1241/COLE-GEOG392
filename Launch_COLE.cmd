@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo COLE's development environment is missing. Use the portable distribution or follow README.md.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "run_cole.py"
