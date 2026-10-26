@echo off
cd /d "%~dp0"
py -3 -m pip install --require-hashes -r requirements-desktop.lock
if errorlevel 1 exit /b 1
echo Desktop dependencies installed.
pause
