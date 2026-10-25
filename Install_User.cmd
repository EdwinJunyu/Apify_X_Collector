@echo off
cd /d "%~dp0"
py -3 manage_release.py install
if errorlevel 1 exit /b 1
pause
