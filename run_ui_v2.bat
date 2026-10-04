@echo off
setlocal
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo [ERROR] .venv not found. Run setup_windows.bat first.
  pause
  exit /b 1
)
set "SHOP_SOURCE_PY=%CD%\.venv\Scripts\python.exe"
"%SHOP_SOURCE_PY%" -c "import PIL" >nul 2>&1
if errorlevel 1 (
  echo [NOTICE] %CD%\.venv is missing Pillow. It is required for image inspection.
  choice /c YN /m "Install declared ShopSource runtime dependencies with this interpreter now?"
  if errorlevel 2 goto launch
  "%SHOP_SOURCE_PY%" -m pip install -e ".[ui]"
  if errorlevel 1 (
    echo [ERROR] Dependency repair failed. Check network access and .venv permissions.
    pause
    exit /b 1
  )
  echo Pillow is installed. Starting ShopSource now.
)
:launch
"%SHOP_SOURCE_PY%" -m shopsource.ui.v2
