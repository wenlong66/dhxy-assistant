@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/3] Create Python 3.11 environment...
py -3.11 -m venv .venv
if errorlevel 1 goto :fail
echo [2/3] Install GUI and device libraries...
".venv\Scripts\python.exe" -m pip install -r requirements-gui.txt
if errorlevel 1 goto :fail
echo [3/3] Download and verify OCR models...
".venv\Scripts\python.exe" setup_ocr.py
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" setup_ocr.py --verify
if errorlevel 1 goto :fail
echo.
echo Installation completed. Run start_gui.bat.
pause
exit /b 0
:fail
echo.
echo Installation failed. Review the messages above. Do not disable TLS checks.
pause
exit /b 1
