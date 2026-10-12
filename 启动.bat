@echo off
cd /d "%~dp0"
if exist "%~dp0dist\CQU-Net-Switch.exe" (
    start "" "%~dp0dist\CQU-Net-Switch.exe"
    exit /b
)
python tray.py
if errorlevel 1 pause
