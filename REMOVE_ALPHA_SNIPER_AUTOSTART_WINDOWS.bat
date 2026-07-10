@echo off
setlocal EnableExtensions

echo Removing Alpha Sniper Flow auto-start...
echo.

schtasks /Delete /TN "Alpha Sniper Flow VPS" /F
if errorlevel 1 (
  echo.
  echo Auto-start task was not found or could not be removed.
  pause
  exit /b 1
)

echo.
echo Auto-start removed.
pause
