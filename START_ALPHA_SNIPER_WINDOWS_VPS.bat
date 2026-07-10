@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Alpha Sniper Flow VPS

cd /d "%~dp0"

echo Starting Alpha Sniper Flow on Windows VPS...
echo Keep this window open while the scanner is running.
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

echo Installing/checking scanner packages...
"backend\.venv\Scripts\python.exe" -m pip install --upgrade pip >nul
"backend\.venv\Scripts\python.exe" -m pip install -r backend\requirements.txt >nul
if errorlevel 1 (
  echo Package install failed. Check VPS internet connection, then run this file again.
  pause
  exit /b 1
)

echo Opening Windows Firewall TCP port 8000 if allowed...
netsh advfirewall firewall add rule name="Alpha Sniper Flow 8000" dir=in action=allow protocol=TCP localport=8000 >nul 2>nul

echo Stopping old local scanner processes if any...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /F /PID %%P >nul 2>nul
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8099" ^| findstr "LISTENING"') do taskkill /F /PID %%P >nul 2>nul

set "PUBLIC_IP="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "try { (Invoke-RestMethod -UseBasicParsing https://api.ipify.org).Trim() } catch { '' }"`) do set "PUBLIC_IP=%%I"

echo Starting scanner app for public VPS access on port 8000...
start "Alpha Sniper VPS App" /min cmd /c "cd /d ""%~dp0backend"" && .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000"

echo Waiting for scanner to start...
for /l %%I in (1,1,40) do (
  powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/health | Out-Null; Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/spot-momentum-scanner.html | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
  if !ERRORLEVEL! EQU 0 goto READY
  timeout /t 1 >nul
)

echo.
echo The scanner did not finish starting yet.
echo Check Python errors, VPS internet connection, then run this file again.
pause
exit /b 1

:READY
set "LOCAL_URL=http://localhost:8000/spot-momentum-scanner.html?v=%RANDOM%%RANDOM%"
if defined PUBLIC_IP set "PUBLIC_URL=http://%PUBLIC_IP%:8000/spot-momentum-scanner.html"

powershell -NoProfile -Command "Start-Process '%LOCAL_URL%'" >nul 2>nul
if errorlevel 1 start "" "%LOCAL_URL%"

echo.
echo Alpha Sniper Flow is running on this Windows VPS.
echo.
echo VPS local browser link:
echo %LOCAL_URL%
if defined PUBLIC_URL (
  echo.
echo Public link for your phone, Mac, Windows, iPhone, Android:
echo %PUBLIC_URL%
)
echo.
echo Admin panel:
if defined PUBLIC_IP (
  echo http://%PUBLIC_IP%:8000/admin.html
) else (
  echo http://localhost:8000/admin.html
)
echo.
echo Owner password file:
echo %~dp0ADMIN_PASSWORD.txt
echo.
echo If public link does not open:
echo 1. Open TCP port 8000 in your VPS provider firewall/security group.
echo 2. Make sure Windows Firewall allowed TCP 8000.
echo 3. Keep this VPS window open.
echo.
echo User dashboard is protected by PIN login.
echo Keep ADMIN_PASSWORD.txt private.
echo.
pause
