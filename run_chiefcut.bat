@echo off
REM ChiefCut launcher - double-click this file to start ChiefCut.
setlocal
cd /d "%~dp0"
title ChiefCut

if exist ".venv\Scripts\python.exe" goto have_venv
echo [ChiefCut] First run: creating a private Python environment in .venv ...
where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo [ChiefCut] Python was not found. Install it from PowerShell with:
  echo     winget install -e --id Python.Python.3.12
  echo Then close this window and double-click run_chiefcut.bat again.
  pause
  exit /b 1
)
:have_venv

fc /b requirements.txt .venv.installed >nul 2>nul
if errorlevel 1 (
  echo [ChiefCut] Installing / updating packages - this takes a minute the first time ...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 (
    echo [ChiefCut] Package installation failed. Check your internet connection and try again.
    pause
    exit /b 1
  )
  copy /y requirements.txt .venv.installed >nul
)

".venv\Scripts\python.exe" -m chiefcut.doctor
if errorlevel 1 (
  echo.
  echo [ChiefCut] Please fix the problem above, then run this file again.
  pause
  exit /b 1
)

echo.
echo [ChiefCut] Starting at http://localhost:8501  - keep this window open while you use ChiefCut.
echo [ChiefCut] To stop ChiefCut, close this window or press Ctrl+C.
".venv\Scripts\python.exe" -m streamlit run app.py --server.address 127.0.0.1
pause
