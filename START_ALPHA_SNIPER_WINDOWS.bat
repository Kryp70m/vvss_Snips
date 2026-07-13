@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Alpha Sniper Flow

cd /d "%~dp0"

echo Starting Alpha Sniper Flow...
echo This window must stay open while you use the scanner.
echo.

set "PYTHON_CMD="
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3.12 -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)" >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=py -3.12"
  if not defined PYTHON_CMD (
    py -3.11 -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3.11"
  )
  if not defined PYTHON_CMD (
    py -3.10 -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3.10"
  )
)
if not defined PYTHON_CMD (
  where python >nul 2>nul
  if %ERRORLEVEL%==0 (
    python -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
  )
)
if not defined PYTHON_CMD (
  echo Python 3.10, 3.11, or 3.12 is required.
  echo Python 3.13/3.14 is too new for some Windows scanner packages.
  echo Install Python 3.12 from https://www.python.org/downloads/release/python-3128/ and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

if exist "backend\.venv\Scripts\python.exe" (
  "backend\.venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)" >nul 2>nul
  if errorlevel 1 (
    echo Existing Python environment is not compatible. Rebuilding it with Python 3.10-3.12...
    rmdir /s /q backend\.venv
  )
)

if not exist "backend\.venv\Scripts\python.exe" (
  echo Creating local Python environment...
  %PYTHON_CMD% -m venv backend\.venv
  if errorlevel 1 (
    echo Could not create Python environment.
    pause
    exit /b 1
  )
)

echo Installing/checking backend packages...
"backend\.venv\Scripts\python.exe" -m pip install --upgrade pip >nul
"backend\.venv\Scripts\python.exe" -m pip install -r backend\requirements.txt >nul
if errorlevel 1 (
  echo Package install failed. Check your internet connection, then run this file again.
  pause
  exit /b 1
)

echo Stopping old local scanner processes if any...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /F /PID %%P >nul 2>nul
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8099" ^| findstr "LISTENING"') do taskkill /F /PID %%P >nul 2>nul

set "LAN_IP="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "($ip = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -ne 'WellKnown' } | Select-Object -First 1 -ExpandProperty IPAddress); if ($ip) { $ip }"`) do set "LAN_IP=%%I"

echo Starting scanner app on http://localhost:8000 ...
start "Alpha Sniper App" /min cmd /c "cd /d ""%~dp0backend"" && .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000"

echo Waiting for scanner to start...
for /l %%I in (1,1,30) do (
  powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/health | Out-Null; Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/spot-momentum-scanner.html | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
  if !ERRORLEVEL! EQU 0 goto READY
  timeout /t 1 >nul
)

echo.
echo The scanner did not finish starting yet.
echo Please check your internet connection and any Python error shown above, then run this file again.
pause
exit /b 1

:READY
set "APP_URL=http://localhost:8000/spot-momentum-scanner.html?v=%RANDOM%%RANDOM%"
powershell -NoProfile -Command "Start-Process '%APP_URL%'" >nul 2>nul
if errorlevel 1 start "" "%APP_URL%"

echo.
echo Alpha Sniper Flow is running.
echo Computer link:
echo %APP_URL%
echo.
echo If the browser did not open, double-click:
echo OPEN_WEBSITE_WINDOWS.bat
if defined LAN_IP (
  echo.
  echo Phone / tablet / iPad / iPhone / Android link on the same Wi-Fi:
  echo http://%LAN_IP%:8000/spot-momentum-scanner.html
)
echo.
echo If Windows Firewall asks, allow Python on Private Networks.
echo Do not close the Alpha Sniper App window while testing.
echo.
echo To stop later, double-click STOP_ALPHA_SNIPER_WINDOWS.bat or close the Python windows.
pause
