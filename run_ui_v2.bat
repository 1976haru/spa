@echo off
setlocal
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo [ERROR] .venv not found. Run setup_windows.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m shopsource.ui.v2
