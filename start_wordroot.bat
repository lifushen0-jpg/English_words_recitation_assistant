@echo off
chcp 65001 >nul
cd /d "%~dp0"
title WordRoot Server

where python >nul 2>nul
if errorlevel 1 (
  echo 未检测到 Python。请先安装 Python 3.10 或更高版本。
  pause
  exit /b 1
)

start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Milliseconds 900; Start-Process 'http://127.0.0.1:8000'"
python server.py

if errorlevel 1 (
  echo.
  echo WordRoot 未能启动，请查看上方提示。
  pause
)
