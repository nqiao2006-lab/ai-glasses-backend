@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
py -3.11 -c "import sys" >nul 2>&1
if errorlevel 1 (
  set "VISION_PYTHON=python"
) else (
  set "VISION_PYTHON=py -3.11"
)
%VISION_PYTHON% -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) and struct.calcsize('P') == 8 else 1)"
if errorlevel 1 (
  echo Please install 64-bit Python 3.11 and add it to PATH.
  goto failed
)
%VISION_PYTHON% verify_bundle.py
if errorlevel 1 goto failed
%VISION_PYTHON% -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements-api.txt -c constraints-windows-py311.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" patch_paddle_cache.py
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip check
if errorlevel 1 goto failed
echo Installation complete. See README for API startup and Android integration.
pause
exit /b 0
:failed
echo Installation failed. Keep this output for troubleshooting.
pause
exit /b 1
