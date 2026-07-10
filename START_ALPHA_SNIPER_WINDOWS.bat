@echo off
title VolSnipe V2 - Launch Interface
echo Launching VolSnipe V2 Framework via Docker...
cd /d "%~dp0"

# Boot up the container environment in detached mode
docker compose up -d

echo.
echo Waiting for container services to stabilize...
timeout /t 5 >nul

# Open your browser automatically to the correct V2 page
set "APP_URL=http://localhost:8000/spot-momentum-scanner.html"
start "" "%APP_URL%"

echo.
echo V2 Scanner is now running inside Docker.
echo To inspect real-time background logs, run: docker compose logs -f backend
echo.
pause