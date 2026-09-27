@echo off
chcp 65001 >nul
cd /d "%~dp0"
python create_share_package.py
echo.
pause
