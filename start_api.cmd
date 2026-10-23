@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" "serve_vision.py" %*
if errorlevel 1 pause
