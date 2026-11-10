@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo GUI not installed. Run setup_gui.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" gui_app.py
if errorlevel 1 (
  echo GUI exited with an error. Review the console output.
  pause
)
