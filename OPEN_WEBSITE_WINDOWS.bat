@echo off
setlocal EnableExtensions
title Open Alpha Sniper Flow Website

set "APP_URL=http://localhost:8000/spot-momentum-scanner.html?v=%RANDOM%%RANDOM%"

powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/health | Out-Null; Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/spot-momentum-scanner.html | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
if errorlevel 1 (
  echo Alpha Sniper Flow is not running yet.
  echo.
  echo First double-click:
  echo START_ALPHA_SNIPER_WINDOWS.bat
  echo.
  echo Keep that scanner window open, then run this Open Website file again if needed.
  pause
  exit /b 1
)

powershell -NoProfile -Command "Start-Process '%APP_URL%'" >nul 2>nul
if errorlevel 1 start "" "%APP_URL%"
