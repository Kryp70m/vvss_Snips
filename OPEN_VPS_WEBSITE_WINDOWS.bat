@echo off
setlocal EnableExtensions
title Open Alpha Sniper Flow VPS Website

set "PUBLIC_IP="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "try { (Invoke-RestMethod -UseBasicParsing https://api.ipify.org).Trim() } catch { '' }"`) do set "PUBLIC_IP=%%I"

if not defined PUBLIC_IP (
  echo Could not detect VPS public IP.
  echo Open this on the VPS browser:
  echo http://localhost:8000/spot-momentum-scanner.html
  pause
  exit /b 1
)

set "APP_URL=http://%PUBLIC_IP%:8000/spot-momentum-scanner.html"
powershell -NoProfile -Command "Start-Process '%APP_URL%'" >nul 2>nul
if errorlevel 1 start "" "%APP_URL%"

echo Public website link:
echo %APP_URL%
pause
