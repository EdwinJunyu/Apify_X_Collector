@echo off
cd /d "%~dp0"
py -3 -m unittest discover -s tests -v
if errorlevel 1 exit /b 1
py -3 -m unittest discover -s legacy/apify_x_probe_v0_1/tests -v
pause
