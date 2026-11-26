@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating Python 3.11 build environment...
  py -3.11 -m venv .venv
  if errorlevel 1 goto :fail
)
".venv\Scripts\python.exe" -X utf8 -c "import sys, struct, platform; sys.exit(0 if sys.platform == 'win32' and struct.calcsize('P') == 8 and platform.machine().upper() in ('AMD64', 'X86_64') else 'Use Windows x64 Python, not 32-bit or ARM64 Python.')"
if errorlevel 1 goto :fail
echo Installing build dependencies...
".venv\Scripts\python.exe" -X utf8 -m pip install -r requirements-build.txt
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -X utf8 setup_ocr.py --verify
if errorlevel 1 (
  ".venv\Scripts\python.exe" -X utf8 setup_ocr.py
  if errorlevel 1 goto :fail
)
".venv\Scripts\python.exe" -X utf8 build_windows.py
if errorlevel 1 goto :fail
echo.
echo Build completed. See the new folder and ZIP in dist.
pause
exit /b 0
:fail
echo.
echo Build failed. Review the messages above. No existing release was deleted.
pause
exit /b 1
