@echo off
setlocal
cd /d %~dp0
py -3.11 -m venv .venv
if errorlevel 1 py -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -e .
python -m shopsource.cli init-db
python -m shopsource.cli add-store stores\001_cabin_tidy.json
python -m shopsource.cli add-store stores\002_garage_fix.json
python -m shopsource.cli add-store stores\003_bathroom_sorted.json
echo.
echo Setup complete. Run run_gui.bat
pause
