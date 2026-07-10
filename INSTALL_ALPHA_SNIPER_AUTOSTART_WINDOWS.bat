@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo Installing Alpha Sniper Flow auto-start...
echo.

set "TASK_NAME=Alpha Sniper Flow VPS"
set "STARTER=%~dp0START_ALPHA_SNIPER_WINDOWS_VPS.bat"

if not exist "%STARTER%" (
  echo START_ALPHA_SNIPER_WINDOWS_VPS.bat was not found.
  pause
  exit /b 1
)

schtasks /Create /TN "%TASK_NAME%" /TR "\"%STARTER%\"" /SC ONSTART /RL HIGHEST /F
if errorlevel 1 (
  echo.
  echo Could not install auto-start. Right-click this file and choose Run as Administrator.
  pause
  exit /b 1
)

echo.
echo Auto-start installed.
echo The platform will start automatically whenever this Windows VPS/computer starts.
echo.
echo Starting it now...
schtasks /Run /TN "%TASK_NAME%" >nul 2>nul

echo.
echo Done.
pause
